from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.jvm_feign import SpringFeignRecognizer
from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    Evidence,
    FlowEdge,
    Injection,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import flows, services
from orbitkb.domain.canonical import ServiceKey
from orbitkb.mcp import queries


def test_feign_recognizer_keeps_call_and_url_binding_together(tmp_path: Path):
    client = tmp_path / "InventoryClient.kt"
    client.write_text(
        '''@FeignClient("inventory", url = $$"${provider.inventory-client.url}")
interface InventoryClient {
    @PostMapping("/reservations/{id}")
    fun reserve(id: String): Reservation
}
''', encoding="utf-8",
    )
    evidence = Evidence("CheckoutService.kt", 4, 4)
    analysis = AnalysisResult(
        edges=[FlowEdge("CheckoutService.checkout", "inventoryClient.reserve", "invokes", evidence)],
        injections=[Injection("CheckoutService.inventoryClient", "InventoryClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert [(call.source, call.target_service, call.target_method, call.target_path) for call in analysis.static_service_calls] == [
        ("CheckoutService.checkout", "inventory", "POST", "/reservations/{id}"),
    ]
    assert [(binding.source, binding.key, binding.kind) for binding in analysis.configuration_bindings] == [
        ("InventoryClient", "provider.inventory-client.url", "property"),
    ]


def test_feign_recognizer_ignores_mapping_in_comment_and_string(tmp_path: Path):
    client = tmp_path / "InventoryClient.kt"
    client.write_text(
        '''@FeignClient("inventory")
interface InventoryClient {
    // @GetMapping("/ghost-comment")
    fun fromComment(): Item

    /* outer /* inner */
       @GetMapping("/ghost-block")
       fun fromBlock(): Item
    */
    fun fromBlock(): Item

    fun example(): String = """
        @GetMapping("/ghost-string")
        fun fromString(): Item
    """
    fun fromString(): Item

    @GetMapping("/real")
    fun real(): Item
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.kt", 4, 4)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("CatalogService.read", f"inventoryClient.{method}", "invokes", evidence)
            for method in ("fromComment", "fromBlock", "fromString", "real")
        ],
        injections=[Injection("CatalogService.inventoryClient", "InventoryClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert [(call.target_method, call.target_path) for call in analysis.static_service_calls] == [
        ("GET", "/real"),
    ]


def test_feign_recognizer_ignores_commented_client_declaration(tmp_path: Path):
    client = tmp_path / "Clients.java"
    client.write_text(
        '''// @FeignClient("ghost", url = "${provider.ghost.url}")
interface GhostClient {
    @GetMapping("/ghost")
    Item fetch();
}

@FeignClient("inventory")
interface InventoryClient {
    @GetMapping("/real")
    Item fetch();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("CatalogService.read", "ghostClient.fetch", "invokes", evidence),
            FlowEdge("CatalogService.read", "inventoryClient.fetch", "invokes", evidence),
        ],
        injections=[
            Injection("CatalogService.ghostClient", "GhostClient", None, evidence),
            Injection("CatalogService.inventoryClient", "InventoryClient", None, evidence),
        ],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert [(call.target_service, call.target_path) for call in analysis.static_service_calls] == [
        ("inventory", "/real"),
    ]
    assert analysis.configuration_bindings == []


def test_feign_recognizer_does_not_claim_dynamic_service_name(tmp_path: Path):
    client = tmp_path / "Clients.java"
    client.write_text(
        '''@FeignClient(name = "${inventory.service}", url = "${provider.inventory.url}")
interface ConfiguredClient {
    @GetMapping("/items")
    Item getItem();
}

@FeignClient(name = "#{services.inventory}")
interface ExpressionClient {
    @GetMapping("/items")
    Item getItem();
}
''', encoding="utf-8",
    )
    kotlin_client = tmp_path / "KotlinClient.kt"
    kotlin_client.write_text(
        '''@FeignClient(name = "$inventoryService")
interface KotlinClient {
    @GetMapping("/items")
    fun getItem(): Item
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("CatalogService.read", "configuredClient.getItem", "invokes", evidence),
            FlowEdge("CatalogService.read", "expressionClient.getItem", "invokes", evidence),
            FlowEdge("CatalogService.read", "kotlinClient.getItem", "invokes", evidence),
        ],
        injections=[
            Injection("CatalogService.configuredClient", "ConfiguredClient", None, evidence),
            Injection("CatalogService.expressionClient", "ExpressionClient", None, evidence),
            Injection("CatalogService.kotlinClient", "KotlinClient", None, evidence),
        ],
    )

    SpringFeignRecognizer().enrich(analysis, [client, kotlin_client], tmp_path)

    assert analysis.static_service_calls == []
    assert [(binding.source, binding.key) for binding in analysis.configuration_bindings] == [
        ("ConfiguredClient", "provider.inventory.url"),
    ]


def test_feign_recognizer_does_not_choose_between_overloaded_mappings(tmp_path: Path):
    client = tmp_path / "InventoryClient.java"
    client.write_text(
        '''@FeignClient("inventory")
interface InventoryClient {
    @GetMapping("/items/by-id")
    Item fetch(String id);

    @GetMapping("/items/by-sku")
    Item fetch(int sku);

    @GetMapping("/health")
    Status health();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("CatalogService.read", "inventoryClient.fetch", "invokes", evidence),
            FlowEdge("CatalogService.read", "inventoryClient.health", "invokes", evidence),
        ],
        injections=[Injection("CatalogService.inventoryClient", "InventoryClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert [(call.target_method, call.target_path) for call in analysis.static_service_calls] == [
        ("GET", "/health"),
    ]


def test_feign_recognizer_resolves_homonymous_interfaces_from_explicit_imports(tmp_path: Path):
    first = tmp_path / "first" / "InventoryClient.java"
    first.parent.mkdir()
    first.write_text(
        '''package first;
@FeignClient("primary-inventory")
interface InventoryClient {
    @GetMapping("/primary/items")
    Item fetch();
}
''', encoding="utf-8",
    )
    second = tmp_path / "second" / "InventoryClient.java"
    second.parent.mkdir()
    second.write_text(
        '''package second;
@FeignClient("secondary-inventory")
interface InventoryClient {
    @GetMapping("/secondary/items")
    Item fetch();
}
''', encoding="utf-8",
    )
    primary_consumer = tmp_path / "PrimaryService.java"
    primary_consumer.write_text(
        '''import first.InventoryClient;
class PrimaryService { private InventoryClient client; }
''', encoding="utf-8",
    )
    secondary_consumer = tmp_path / "SecondaryService.java"
    secondary_consumer.write_text(
        '''import second.InventoryClient;
class SecondaryService { private InventoryClient client; }
''', encoding="utf-8",
    )
    unknown_consumer = tmp_path / "UnknownService.java"
    unknown_consumer.write_text(
        '''import first.*;
import second.*;
/*
import second.InventoryClient;
*/
class UnknownService { private InventoryClient client; }
''', encoding="utf-8",
    )
    analysis = AnalysisResult(
        edges=[
            FlowEdge(f"{owner}.read", "client.fetch", "invokes", Evidence(f"{owner}.java", 2, 2))
            for owner in ("PrimaryService", "SecondaryService", "UnknownService")
        ],
        injections=[
            Injection(f"{owner}.client", "InventoryClient", None, Evidence(f"{owner}.java", 2, 2))
            for owner in ("PrimaryService", "SecondaryService", "UnknownService")
        ],
    )

    SpringFeignRecognizer().enrich(
        analysis, [first, second, primary_consumer, secondary_consumer, unknown_consumer], tmp_path,
    )

    assert [(call.source, call.target_service, call.target_path) for call in analysis.static_service_calls] == [
        ("PrimaryService.read", "primary-inventory", "/primary/items"),
        ("SecondaryService.read", "secondary-inventory", "/secondary/items"),
    ]


def test_feign_recognizer_resolves_a_fully_qualified_injection_type(tmp_path: Path):
    client = tmp_path / "InventoryClient.java"
    client.write_text(
        '''package inventory;
@FeignClient("inventory-service")
interface InventoryClient {
    @GetMapping("/items")
    Item fetch();
}
''', encoding="utf-8",
    )
    consumer = tmp_path / "CatalogService.kt"
    consumer.write_text(
        '''package catalog
class CatalogService(private val client: inventory.InventoryClient)
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.kt", 2, 2)
    analysis = AnalysisResult(
        edges=[FlowEdge("CatalogService.read", "client.fetch", "invokes", evidence)],
        injections=[Injection("CatalogService.client", "inventory.InventoryClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client, consumer], tmp_path)

    assert [(call.target_service, call.target_path) for call in analysis.static_service_calls] == [
        ("inventory-service", "/items"),
    ]


def test_feign_url_bindings_keep_homonymous_clients_distinct_in_public_configuration(tmp_path: Path):
    files = []
    for package in ("first", "second"):
        client = tmp_path / package / "InventoryClient.java"
        client.parent.mkdir()
        client.write_text(
            f'''package {package};
@FeignClient(name = "{package}-inventory", url = "${{provider.shared.url}}")
interface InventoryClient {{
    @GetMapping("/items")
    Item fetch();
}}
''', encoding="utf-8",
        )
        files.append(client)
    analysis = AnalysisResult()

    SpringFeignRecognizer().enrich(analysis, files, tmp_path)

    assert [(binding.source, binding.key) for binding in analysis.configuration_bindings] == [
        ("first.InventoryClient", "provider.shared.url"),
        ("second.InventoryClient", "provider.shared.url"),
    ]
    snapshot = project_analysis(ServiceKey("catalog"), analysis)
    assert len([fact for fact in snapshot.facts if fact.kind == "configuration"]) == 2

    conn = open_db(tmp_path / "catalog.db")
    service_id = services.ensure_service(conn, "catalog", str(tmp_path), "jvm-spring")
    flows.replace_analysis(conn, service_id, analysis)

    assert [binding["source"] for binding in queries.describe_configuration(conn, "catalog")["bindings"]] == [
        "first.InventoryClient", "second.InventoryClient",
    ]


def test_feign_literal_public_url_is_an_external_http_call(tmp_path: Path):
    client = tmp_path / "VendorClient.java"
    client.write_text(
        '''@FeignClient(name = "catalog-service", url = "https://api.vendor.example/v1")
interface VendorClient {
    @GetMapping("/items")
    Item fetch();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence)],
        injections=[Injection("CatalogService.vendorClient", "VendorClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert analysis.static_service_calls == []
    assert [
        (call.source, call.scheme, call.host, call.port, call.method, call.path)
        for call in analysis.external_http_calls
    ] == [("CatalogService.read", "https", "api.vendor.example", None, "GET", "/v1/items")]
    assert [fact.kind for fact in project_analysis(ServiceKey("catalog"), analysis).facts if fact.kind.endswith("call")] == [
        "external_http_call",
    ]
    analysis.entrypoints.append(EntryPoint("http", "GET", "/catalog", "CatalogService.read", evidence))
    conn = open_db(tmp_path / "catalog.db")
    service_id = services.ensure_service(conn, "catalog", str(tmp_path), "jvm-spring")
    flows.replace_analysis(conn, service_id, analysis)
    detail = queries.describe_entrypoint(conn, "catalog", "http", "get", "/catalog")
    assert detail["service_calls"] == []
    assert [(call["host"], call["path"]) for call in detail["external_http_calls"]] == [
        ("api.vendor.example", "/v1/items"),
    ]


def test_feign_unclassifiable_literal_url_does_not_become_an_internal_service_call(tmp_path: Path):
    client = tmp_path / "VendorClient.java"
    client.write_text(
        '''@FeignClient(name = "catalog-service", url = "https://user:secret@api.vendor.example/v1")
interface VendorClient {
    @GetMapping("/items")
    Item fetch();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence)],
        injections=[Injection("CatalogService.vendorClient", "VendorClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert analysis.static_service_calls == []
    assert analysis.external_http_calls == []


def test_feign_clients_with_same_name_and_route_keep_distinct_external_hosts(tmp_path: Path):
    clients = []
    for client_name, host in (("FirstClient", "first.example"), ("SecondClient", "second.example")):
        client = tmp_path / f"{client_name}.java"
        client.write_text(
            f'''@FeignClient(name = "vendor", url = "https://{host}")
interface {client_name} {{
    @GetMapping("/items")
    Item fetch();
}}
''', encoding="utf-8",
        )
        clients.append(client)
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("CatalogService.read", "firstClient.fetch", "invokes", evidence),
            FlowEdge("CatalogService.read", "secondClient.fetch", "invokes", evidence),
        ],
        injections=[
            Injection("CatalogService.firstClient", "FirstClient", None, evidence),
            Injection("CatalogService.secondClient", "SecondClient", None, evidence),
        ],
    )

    SpringFeignRecognizer().enrich(analysis, clients, tmp_path)

    assert sorted(call.host for call in analysis.external_http_calls) == ["first.example", "second.example"]


def test_feign_composed_dynamic_urls_do_not_claim_a_destination(tmp_path: Path):
    client = tmp_path / "VendorClients.java"
    client.write_text(
        '''@FeignClient(name = "catalog-service", url = "https://${provider.host}/v1")
interface DynamicHostClient {
    @GetMapping("/items")
    Item fetch();
}

@FeignClient(name = "catalog-service", url = "https://api.vendor.example/${tenant}")
interface DynamicPathClient {
    @GetMapping("/items")
    Item fetch();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("CatalogService.read", "dynamicHostClient.fetch", "invokes", evidence),
            FlowEdge("CatalogService.read", "dynamicPathClient.fetch", "invokes", evidence),
        ],
        injections=[
            Injection("CatalogService.dynamicHostClient", "DynamicHostClient", None, evidence),
            Injection("CatalogService.dynamicPathClient", "DynamicPathClient", None, evidence),
        ],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert analysis.static_service_calls == []
    assert analysis.external_http_calls == []


def test_feign_public_url_joins_interface_and_method_prefixes(tmp_path: Path):
    client = tmp_path / "VendorClient.java"
    client.write_text(
        '''@FeignClient(name = "catalog-service", url = "https://api.vendor.example/api")
@RequestMapping("/v1")
interface VendorClient {
    @GetMapping("/items/{itemId}")
    Item fetch();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence)],
        injections=[Injection("CatalogService.vendorClient", "VendorClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert analysis.static_service_calls == []
    assert [(call.host, call.path) for call in analysis.external_http_calls] == [
        ("api.vendor.example", "/api/v1/items/{itemId}"),
    ]


def test_kotlin_feign_interpolated_url_path_does_not_claim_a_literal_path(tmp_path: Path):
    client = tmp_path / "VendorClient.kt"
    client.write_text(
        '''@FeignClient(name = "catalog-service", url = "https://api.vendor.example/$tenant")
interface VendorClient {
    @GetMapping("/items")
    fun fetch(): Item
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.kt", 4, 4)
    analysis = AnalysisResult(
        edges=[FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence)],
        injections=[Injection("CatalogService.vendorClient", "VendorClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert analysis.static_service_calls == []
    assert analysis.external_http_calls == []


def test_feign_url_constants_do_not_claim_the_declared_name_as_a_destination(tmp_path: Path):
    java_client = tmp_path / "JavaVendorClient.java"
    java_client.write_text(
        '''@FeignClient(name = "catalog-service", url = VendorSettings.API_URL)
interface JavaVendorClient {
    @GetMapping("/items")
    Item fetch();
}
''', encoding="utf-8",
    )
    kotlin_client = tmp_path / "KotlinVendorClient.kt"
    kotlin_client.write_text(
        '''@FeignClient(name = "catalog-service", url = VENDOR_URL)
interface KotlinVendorClient {
    @GetMapping("/items")
    fun fetch(): Item
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("CatalogService.read", "javaVendorClient.fetch", "invokes", evidence),
            FlowEdge("CatalogService.read", "kotlinVendorClient.fetch", "invokes", evidence),
        ],
        injections=[
            Injection("CatalogService.javaVendorClient", "JavaVendorClient", None, evidence),
            Injection("CatalogService.kotlinVendorClient", "KotlinVendorClient", None, evidence),
        ],
    )

    SpringFeignRecognizer().enrich(analysis, [java_client, kotlin_client], tmp_path)

    assert analysis.static_service_calls == []
    assert analysis.external_http_calls == []
    assert analysis.configuration_bindings == []


def test_feign_composed_url_arguments_are_not_treated_as_complete_literals(tmp_path: Path):
    client = tmp_path / "VendorClients.java"
    client.write_text(
        '''@FeignClient(name = "catalog-service", url = "https://api.vendor.example" + VendorSettings.PATH)
interface LiteralPrefixClient {
    @GetMapping("/items")
    Item fetch();
}

@FeignClient(name = "catalog-service", url = "${provider.vendor.url}" + "/v1")
interface PropertyPrefixClient {
    @GetMapping("/items")
    Item fetch();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("CatalogService.read", "literalPrefixClient.fetch", "invokes", evidence),
            FlowEdge("CatalogService.read", "propertyPrefixClient.fetch", "invokes", evidence),
        ],
        injections=[
            Injection("CatalogService.literalPrefixClient", "LiteralPrefixClient", None, evidence),
            Injection("CatalogService.propertyPrefixClient", "PropertyPrefixClient", None, evidence),
        ],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert analysis.static_service_calls == []
    assert analysis.external_http_calls == []
    assert analysis.configuration_bindings == []


def test_feign_empty_url_keeps_the_declared_service_name(tmp_path: Path):
    client = tmp_path / "VendorClient.java"
    client.write_text(
        '''@FeignClient(name = "catalog-service", url = "")
interface VendorClient {
    @GetMapping("/items")
    Item fetch();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence)],
        injections=[Injection("CatalogService.vendorClient", "VendorClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert [(call.target_service, call.target_path) for call in analysis.static_service_calls] == [
        ("catalog-service", "/items"),
    ]
    assert analysis.external_http_calls == []


def test_feign_url_before_name_keeps_external_destination_and_property_binding(tmp_path: Path):
    java_client = tmp_path / "VendorClient.java"
    java_client.write_text(
        '''@FeignClient(url = "https://api.vendor.example/v1", name = "catalog-service")
@RequestMapping("/catalog")
interface VendorClient {
    @GetMapping("/items")
    Item fetch();
}
''', encoding="utf-8",
    )
    kotlin_client = tmp_path / "InventoryClient.kt"
    kotlin_client.write_text(
        '''@FeignClient(url = $$"${provider.inventory.url}", value = "inventory-service")
interface InventoryClient {
    @PostMapping("/reservations")
    fun reserve(): Reservation
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence),
            FlowEdge("CatalogService.read", "inventoryClient.reserve", "invokes", evidence),
        ],
        injections=[
            Injection("CatalogService.vendorClient", "VendorClient", None, evidence),
            Injection("CatalogService.inventoryClient", "InventoryClient", None, evidence),
        ],
    )

    SpringFeignRecognizer().enrich(analysis, [java_client, kotlin_client], tmp_path)

    assert [(call.host, call.path) for call in analysis.external_http_calls] == [
        ("api.vendor.example", "/v1/catalog/items"),
    ]
    assert [(call.target_service, call.target_path) for call in analysis.static_service_calls] == [
        ("inventory-service", "/reservations"),
    ]
    assert [(binding.source, binding.key) for binding in analysis.configuration_bindings] == [
        ("InventoryClient", "provider.inventory.url"),
    ]


def test_feign_url_parenthesis_inside_literal_keeps_the_complete_annotation(tmp_path: Path):
    client = tmp_path / "VendorClient.java"
    client.write_text(
        '''@FeignClient(url = "https://api.vendor.example/v1/(archive)", name = "catalog-service")
@RequestMapping("/catalog/(archived)")
interface VendorClient {
    @GetMapping("/items")
    Item fetch();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence)],
        injections=[Injection("CatalogService.vendorClient", "VendorClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert analysis.static_service_calls == []
    assert [(call.host, call.path) for call in analysis.external_http_calls] == [
        ("api.vendor.example", "/v1/(archive)/catalog/(archived)/items"),
    ]


def test_feign_method_annotation_with_parenthesis_in_headers_keeps_the_full_path(tmp_path: Path):
    client = tmp_path / "VendorClient.java"
    client.write_text(
        '''@FeignClient(name = "catalog-service", url = "https://api.vendor.example/v1")
interface VendorClient {
    @GetMapping(value = "/items/(archived)", headers = "X-View=(archived)")
    Item fetch();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence)],
        injections=[Injection("CatalogService.vendorClient", "VendorClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert analysis.static_service_calls == []
    assert [(call.host, call.path) for call in analysis.external_http_calls] == [
        ("api.vendor.example", "/v1/items/(archived)"),
    ]


def test_feign_named_path_is_resolved_independently_of_mapping_argument_order(tmp_path: Path):
    client = tmp_path / "VendorClient.java"
    client.write_text(
        '''@FeignClient(name = "catalog-service", url = "https://api.vendor.example/v1")
interface VendorClient {
    @GetMapping(path = "/items")
    Item fetch();

    @PostMapping(produces = "application/json", path = "/reservations")
    Reservation reserve();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence),
            FlowEdge("CatalogService.read", "vendorClient.reserve", "invokes", evidence),
        ],
        injections=[Injection("CatalogService.vendorClient", "VendorClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert analysis.static_service_calls == []
    assert [(call.method, call.path) for call in analysis.external_http_calls] == [
        ("GET", "/v1/items"),
        ("POST", "/v1/reservations"),
    ]


def test_feign_conflicting_named_paths_do_not_choose_an_arbitrary_route(tmp_path: Path):
    client = tmp_path / "VendorClient.java"
    client.write_text(
        '''@FeignClient(name = "catalog-service")
interface VendorClient {
    @GetMapping(value = "/items", path = "/other-items")
    Item fetch();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.java", 4, 4)
    analysis = AnalysisResult(
        edges=[FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence)],
        injections=[Injection("CatalogService.vendorClient", "VendorClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert analysis.static_service_calls == []
    assert analysis.external_http_calls == []


def test_feign_single_route_arrays_resolve_for_kotlin_and_java(tmp_path: Path):
    kotlin_client = tmp_path / "KotlinVendorClient.kt"
    kotlin_client.write_text(
        '''@FeignClient(name = "catalog-service")
interface KotlinVendorClient {
    @GetMapping(path = ["/items"])
    fun fetch(): Item
}
''', encoding="utf-8",
    )
    java_client = tmp_path / "JavaVendorClient.java"
    java_client.write_text(
        '''@FeignClient(name = "inventory-service")
interface JavaVendorClient {
    @PostMapping(value = {"/reservations"})
    Reservation reserve();
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.kt", 4, 4)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("CatalogService.read", "kotlinVendorClient.fetch", "invokes", evidence),
            FlowEdge("CatalogService.read", "javaVendorClient.reserve", "invokes", evidence),
        ],
        injections=[
            Injection("CatalogService.kotlinVendorClient", "KotlinVendorClient", None, evidence),
            Injection("CatalogService.javaVendorClient", "JavaVendorClient", None, evidence),
        ],
    )

    SpringFeignRecognizer().enrich(analysis, [kotlin_client, java_client], tmp_path)

    assert [(call.target_service, call.target_method, call.target_path) for call in analysis.static_service_calls] == [
        ("catalog-service", "GET", "/items"),
        ("inventory-service", "POST", "/reservations"),
    ]


def test_feign_multiple_route_array_remains_unresolved(tmp_path: Path):
    client = tmp_path / "VendorClient.kt"
    client.write_text(
        '''@FeignClient(name = "catalog-service")
interface VendorClient {
    @GetMapping(path = ["/items", "/legacy-items"])
    fun fetch(): Item
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.kt", 4, 4)
    analysis = AnalysisResult(
        edges=[FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence)],
        injections=[Injection("CatalogService.vendorClient", "VendorClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert analysis.static_service_calls == []
    assert analysis.external_http_calls == []


def test_feign_interface_single_route_array_composes_with_method_path(tmp_path: Path):
    client = tmp_path / "VendorClient.kt"
    client.write_text(
        '''@FeignClient(name = "catalog-service")
@RequestMapping(path = ["/v1"])
interface VendorClient {
    @GetMapping("/items")
    fun fetch(): Item
}
''', encoding="utf-8",
    )
    evidence = Evidence("CatalogService.kt", 4, 4)
    analysis = AnalysisResult(
        edges=[FlowEdge("CatalogService.read", "vendorClient.fetch", "invokes", evidence)],
        injections=[Injection("CatalogService.vendorClient", "VendorClient", None, evidence)],
    )

    SpringFeignRecognizer().enrich(analysis, [client], tmp_path)

    assert [(call.target_service, call.target_path) for call in analysis.static_service_calls] == [
        ("catalog-service", "/v1/items"),
    ]
