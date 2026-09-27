"""Test code isn't production behavior: no HTTP entrypoint, no real call into a
mocked repository/persistence layer, nothing that runs at runtime. Analyzing it
adds noise to `find_change_surface`/`describe_persistence` (a mocked
`repo.save()` isn't evidence of a real persistence operation) and, in
practice, crashed the tree-sitter walk on a real Kotlin/Spring integration
test file (backtick-named test functions, long MockMvc assertion chains) that
none of this project's own fixtures ever exercised.
"""
from __future__ import annotations

from pathlib import Path

from orbitkb.analysis.engine import StaticAnalysisEngine


def test_excludes_jvm_source_under_a_conventional_test_directory(tmp_path: Path):
    main_dir = tmp_path / "src" / "main" / "kotlin"
    main_dir.mkdir(parents=True)
    (main_dir / "Orders.kt").write_text("class Orders { fun create() { repo.save() } }\n", encoding="utf-8")
    test_dir = tmp_path / "src" / "test" / "kotlin"
    test_dir.mkdir(parents=True)
    (test_dir / "OrdersTest.kt").write_text("class OrdersTest { fun `should crash`() { repo.save() } }\n", encoding="utf-8")

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    names = {symbol.name for symbol in result.symbols}
    assert "Orders.create" in names
    assert not any("OrdersTest" in name for name in names)


def test_excludes_go_files_by_the_standard_test_suffix(tmp_path: Path):
    (tmp_path / "orders.go").write_text(
        'package main\ntype Orders struct{}\nfunc (o *Orders) Create() { o.repo.Save() }\n'
        'func main() { router.POST("/orders", orders.Create) }\n',
        encoding="utf-8",
    )
    (tmp_path / "orders_test.go").write_text(
        "package main\nfunc TestOrders(t *testing.T) { repo.Save() }\n", encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert len(result.entrypoints) == 1
    assert not any(symbol.name.startswith("TestOrders") for symbol in result.symbols)


def test_excludes_node_files_by_test_and_spec_suffixes(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        'import express from "express";\n'
        "const app = express();\n"
        "function createOrder(req: Request, res: Response) {\n"
        "  return orderService.create(req.body);\n"
        "}\n"
        'app.post("/orders", createOrder);\n',
        encoding="utf-8",
    )
    (tmp_path / "orders.test.ts").write_text("test('crashes', () => { repo.save(); });\n", encoding="utf-8")
    (tmp_path / "orders.spec.ts").write_text("describe('orders', () => { repo.save(); });\n", encoding="utf-8")

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert len(result.entrypoints) == 1


def test_excludes_python_files_by_test_prefix_and_suffix(tmp_path: Path):
    (tmp_path / "main.py").write_text("import argparse\n", encoding="utf-8")
    (tmp_path / "test_main.py").write_text("def test_something(): pass\n", encoding="utf-8")
    (tmp_path / "main_test.py").write_text("def test_other(): pass\n", encoding="utf-8")

    files = StaticAnalysisEngine._source_files(tmp_path, ("*.py",))

    assert files == [tmp_path / "main.py"]
