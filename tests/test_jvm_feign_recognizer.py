from pathlib import Path

from orbitkb.analysis.jvm_feign import SpringFeignRecognizer
from orbitkb.analysis.models import AnalysisResult, Evidence, FlowEdge, Injection


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
