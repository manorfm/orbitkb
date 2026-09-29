from pathlib import Path

from orbitkb.analysis.engine import StaticAnalysisEngine


def test_kotlin_spring_empty_get_mapping_uses_class_route(tmp_path: Path):
    (tmp_path / "MenusController.kt").write_text(
        '''import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

@RestController
@RequestMapping("/menus")
class MenusController {
    @GetMapping
    fun all(): List<String> = emptyList()
}
''', encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(entry.method, entry.name, entry.symbol) for entry in result.entrypoints} == {
        ("GET", "/menus", "MenusController.all"),
    }


def test_java_spring_empty_get_mapping_uses_class_route(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''@RestController
@RequestMapping("/orders")
class OrdersController {
    @GetMapping()
    List<String> all() { return List.of(); }
}
''', encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(entry.method, entry.name, entry.symbol) for entry in result.entrypoints} == {
        ("GET", "/orders", "OrdersController.all"),
    }
