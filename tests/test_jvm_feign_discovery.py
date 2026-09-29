from pathlib import Path

from orbitkb.discovery.jvm_stack import endpoint_matches

CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/menu-kotlin-service"


def test_feign_client_mappings_are_not_generation_endpoints():
    routes = {(method, path) for method, path, _, _ in endpoint_matches(CORPUS)}

    assert routes == {("GET", "/menus/{id}"), ("GET", "/menus/by-restaurant/{id}")}


def test_feign_client_and_controller_in_one_file_keep_only_controller_route(tmp_path: Path):
    (tmp_path / "Routes.kt").write_text(
        '''@FeignClient("restaurant-service")
interface RestaurantClient {
    @GetMapping("/restaurants/{id}")
    fun get(id: String): String
}

@RestController
@RequestMapping("/menus")
class MenuController {
    @GetMapping("/{id}")
    fun get(id: String): String = id
}
''', encoding="utf-8",
    )

    routes = {(method, path) for method, path, _, _ in endpoint_matches(tmp_path)}

    assert routes == {("GET", "/menus/{id}")}


def test_bodyless_feign_interface_does_not_hide_following_controller(tmp_path: Path):
    (tmp_path / "Routes.kt").write_text(
        '''@FeignClient("unused")
interface EmptyClient

@RestController
class MenuController {
    @GetMapping("/menus")
    fun all(): String = "ok"
}
''', encoding="utf-8",
    )

    routes = {(method, path) for method, path, _, _ in endpoint_matches(tmp_path)}

    assert routes == {("GET", "/menus")}
