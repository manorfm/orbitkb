"""`orbitkb index/update --verbose` must name the exact file and function being
analyzed at DEBUG level, so that when the process dies (a native crash, not a
catchable Python exception — see the tree-sitter segfault incident this exists
for), the last flushed log line pinpoints where, without needing the user's
source code at all.
"""
from __future__ import annotations

import logging
from pathlib import Path

from orbitkb.analysis.engine import StaticAnalysisEngine


def test_analyze_logs_each_file_at_debug_level(tmp_path: Path, caplog):
    (tmp_path / "main.go").write_text(
        'package main\nfunc main() { router.POST("/orders", orders.Create) }\n', encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG, logger="orbitkb.analysis.engine"):
        StaticAnalysisEngine().analyze(tmp_path, "go")

    assert any("main.go" in record.message for record in caplog.records)


def test_analyze_logs_each_function_symbol_at_debug_level(tmp_path: Path, caplog):
    (tmp_path / "main.go").write_text(
        'package main\ntype Orders struct{}\nfunc (o *Orders) Create() { o.repo.Save() }\n',
        encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG, logger="orbitkb.analysis.engine"):
        StaticAnalysisEngine().analyze(tmp_path, "go")

    assert any("Orders.Create" in record.message for record in caplog.records)


def test_analyze_logs_jvm_spring_functions_too(tmp_path: Path, caplog):
    (tmp_path / "OrdersService.java").write_text(
        "class OrdersService {\n  void create() { repo.save(); }\n}\n", encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG, logger="orbitkb.analysis.engine"):
        StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any("OrdersService.create" in record.message for record in caplog.records)


def test_no_debug_logging_at_default_log_level(tmp_path: Path, caplog):
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="orbitkb.analysis.engine"):
        StaticAnalysisEngine().analyze(tmp_path, "go")

    assert caplog.records == []
