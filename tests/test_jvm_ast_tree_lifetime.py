"""`_parse()` must keep the tree-sitter `Tree` it creates alive for as long as the
`Node`s derived from it are in use, mirroring the fix already applied to
`_FileAnalyzer.parse()` in `orbitkb/analysis/engine.py` (see
`test_engine_tree_lifetime.py`). `Node` objects are views into memory owned by their
`Tree`; `parser.parse(source).root_node` discards the only reference to the `Tree`
in the same expression that returns it, so CPython frees it immediately — before any
subsequent walk over the returned node and its descendants.

`_parse()` here never got that fix and returned a bare `(root_node, source)` tuple,
discarding the `Tree`. That crashed `orbitkb index` with SIGBUS deep inside tree-sitter's
GC-time object traversal while indexing a real Kotlin/Spring service (menu-manager-service),
the same failure mode the `engine.py` incident already documented.
"""
from __future__ import annotations

from tree_sitter import Tree

from orbitkb.discovery.jvm_ast import _parse


def test_parse_retains_the_tree_it_creates(tmp_path):
    java_file = tmp_path / "Foo.java"
    java_file.write_text("class Foo { void bar() {} }")

    parsed = _parse(java_file)

    assert parsed is not None
    tree, root, source = parsed
    assert isinstance(tree, Tree)
    assert tree.root_node == root  # tree-sitter mints a fresh Node wrapper per access
    assert source == java_file.read_bytes()
