"""`_FileAnalyzer.parse()` must keep the tree-sitter `Tree` it creates alive for
as long as the `Node`s derived from it are in use. `Node` objects are views
into memory owned by their `Tree`; `Parser.parse(source).root_node` discards
the only Python reference to the `Tree` in the same expression that returns
it, so CPython's refcounting frees it immediately — before any subsequent
`_walk()`/`_edges_for()` call over the returned node and its descendants.

Whether this actually crashes depends on whether the installed `tree-sitter`
binding happens to keep a `Node` valid independent of its `Tree` staying
referenced — undocumented behavior that varies across versions (this project
pins only lower bounds for tree-sitter/tree-sitter-<language>). A real
`pip install`, on a fresh machine, with newer resolved versions than this
dev environment, segfaulted deep inside `_text()` (`node.start_byte`) while
walking a Kotlin/Spring class — see `orbitkb-plans/` for the incident. This
test can't reliably reproduce a segfault portably (that's exactly the
environment-dependent nature of the bug), so it asserts the actual defect
instead: does `parse()` retain a strong reference to the `Tree` it created?
"""
from __future__ import annotations

import tree_sitter_go
from tree_sitter import Language

from orbitkb.analysis.engine import _FileAnalyzer


def test_parse_retains_the_tree_it_creates():
    analyzer = _FileAnalyzer(Language(tree_sitter_go.language()))

    root = analyzer.parse(b"package main\n\nfunc main() {}\n")

    assert analyzer._tree is not None
    assert analyzer._tree.root_node == root  # tree-sitter mints a fresh Node wrapper per access


def test_parse_still_returns_the_root_node(tmp_path):
    analyzer = _FileAnalyzer(Language(tree_sitter_go.language()))

    root = analyzer.parse(b"package main\n\nfunc main() {}\n")

    assert root.type == "source_file"
