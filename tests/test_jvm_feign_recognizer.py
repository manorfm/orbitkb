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
