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
