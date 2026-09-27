"""`--verbose` must trace `orbitkb index` from its very first step, not just once
per-file static analysis starts: a real crash (see `test_jvm_ast_verbose_logging.py`)
happened during stack detection/hint collection, before `StaticAnalysisEngine.analyze()`
ever ran, so those DEBUG lines were the only thing that could have narrowed it down --
and there weren't any. This covers `discover_services()`'s stack detection, the one
step that runs before any service-specific work exists to log against.
"""
from __future__ import annotations

import logging
from pathlib import Path

from orbitkb.discovery.walker import discover_services


def test_discover_services_logs_each_detected_stack_at_debug_level(tmp_path: Path, caplog):
    (tmp_path / "go.mod").write_text("module example.com/orders\n\ngo 1.21\n", encoding="utf-8")
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="orbitkb.discovery.walker"):
        candidates = discover_services(tmp_path)

    assert candidates
    assert any("go" in record.message for record in caplog.records)
