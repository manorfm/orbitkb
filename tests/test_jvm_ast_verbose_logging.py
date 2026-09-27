"""`resolve_kotlin_java_calls()`/`_parse()` sit directly on the tree-sitter native call
that crashed `orbitkb index` with SIGBUS on a real Kotlin/Spring service -- and that
crash happens during `collect_hints()`, entirely before `StaticAnalysisEngine.analyze()`
ever runs its own per-file DEBUG line (see `test_engine_verbose_logging.py`). So
`--verbose` produced no output at all before the process died: there was nothing in this
module to flush first. These log lines close that gap -- the endpoint file that triggered
resolution, and the exact file about to be handed to tree-sitter's native parser -- so the
next crash's last flushed line names where, without needing the target repo's source.
"""
from __future__ import annotations

import logging

from orbitkb.discovery.jvm_ast import resolve_kotlin_java_calls


def test_resolve_calls_logs_the_endpoint_file_at_debug_level(tmp_path, caplog):
    java_file = tmp_path / "OrdersController.java"
    java_file.write_text(
        "class OrdersController { void create() { save(); } }", encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG, logger="orbitkb.discovery.jvm_ast"):
        resolve_kotlin_java_calls(java_file, tmp_path, "save()", (1, 1))

    assert any("OrdersController.java" in record.message for record in caplog.records)


def test_resolve_calls_logs_each_cross_file_candidate_parsed(tmp_path, caplog):
    (tmp_path / "Helpers.kt").write_text(
        "package a.b\n\nfun helper() {}\n", encoding="utf-8",
    )
    caller = tmp_path / "Caller.kt"
    caller.write_text(
        "package a.b\nimport a.b.helper\n\nclass Caller {\n  fun run() { helper() }\n}\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG, logger="orbitkb.discovery.jvm_ast"):
        excerpts = resolve_kotlin_java_calls(caller, tmp_path, "helper()", (1, 1))

    assert excerpts and excerpts[0].file_path == "Helpers.kt"
    assert any("Helpers.kt" in record.message for record in caplog.records)
