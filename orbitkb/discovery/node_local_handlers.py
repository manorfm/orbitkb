"""Resolve explicitly exported functions behind local Node imports."""

from __future__ import annotations

from pathlib import Path

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from orbitkb.discovery.node_imports import resolve_local_source

_SOURCE_SUFFIXES = (".js", ".ts")
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression"})


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _parse(path: Path, source: bytes) -> Node:
    grammar = tree_sitter_typescript.language_typescript() if path.suffix == ".ts" else tree_sitter_javascript.language()
    return Parser(Language(grammar)).parse(source).root_node


def _declared_functions(declaration: Node, source: bytes) -> frozenset[str]:
    names: set[str] = set()
    if declaration.type == "function_declaration":
        name = declaration.child_by_field_name("name")
        if name is not None:
            names.add(_text(name, source))
    elif declaration.type == "lexical_declaration":
        for variable in declaration.named_children:
            if variable.type != "variable_declarator":
                continue
            name = variable.child_by_field_name("name")
            value = variable.child_by_field_name("value")
            if name is not None and value is not None and value.type in _FUNCTION_VALUES:
                names.add(_text(name, source))
    return frozenset(names)


def _exported_functions(path: Path) -> dict[str, str]:
    source = path.read_bytes()
    tree = _parse(path, source)
    local_functions: set[str] = set()
    for statement in tree.named_children:
        declaration = statement.child_by_field_name("declaration") if statement.type == "export_statement" else statement
        if declaration is not None:
            local_functions.update(_declared_functions(declaration, source))

    exports: dict[str, str] = {}
    for statement in tree.named_children:
        if statement.type != "export_statement":
            continue
        is_default = any(child.type == "default" for child in statement.children)
        if is_default:
            declaration = statement.child_by_field_name("declaration")
            if declaration is not None:
                names = _declared_functions(declaration, source)
                if len(names) == 1:
                    exports["default"] = next(iter(names))
            else:
                identifier = next((child for child in statement.named_children if child.type == "identifier"), None)
                if identifier is not None and _text(identifier, source) in local_functions:
                    exports["default"] = _text(identifier, source)
            continue
        if any(child.type == "type" for child in statement.children):
            continue
        declaration = statement.child_by_field_name("declaration")
        if declaration is not None:
            exports.update({name: name for name in _declared_functions(declaration, source)})
            continue
        if statement.child_by_field_name("source") is not None:
            continue
        clause = next((child for child in statement.named_children if child.type == "export_clause"), None)
        if clause is None:
            continue
        for specifier in clause.named_children:
            if specifier.type != "export_specifier" or any(child.type == "type" for child in specifier.children):
                continue
            name = specifier.child_by_field_name("name")
            alias = specifier.child_by_field_name("alias")
            if name is not None and _text(name, source) in local_functions:
                exports[_text(alias or name, source)] = _text(name, source)
    return exports


def proven_local_handler_imports(tree: Node, source: bytes, path: Path, root: Path) -> dict[str, str]:
    """Map local aliases to analyzed symbols only after a relative import/export proof."""
    root = root.resolve()
    symbols: dict[str, str] = {}
    for statement in tree.named_children:
        if statement.type != "import_statement":
            continue
        clause = next((child for child in statement.named_children if child.type == "import_clause"), None)
        module_node = statement.child_by_field_name("source")
        if clause is None or module_node is None:
            continue
        if any(child.type == "type" for child in statement.children):
            continue
        module = _text(module_node, source)[1:-1]
        imported = resolve_local_source(path, module, root, suffixes=_SOURCE_SUFFIXES)
        if imported is None:
            continue
        exports = _exported_functions(imported)
        for names in clause.named_children:
            if names.type == "identifier":
                local_name = exports.get("default")
                if local_name is not None:
                    symbols[_text(names, source)] = f"{imported.stem}.{local_name}"
                continue
            if names.type != "named_imports":
                continue
            for specifier in names.named_children:
                if specifier.type != "import_specifier" or any(child.type == "type" for child in specifier.children):
                    continue
                original = specifier.child_by_field_name("name")
                alias = specifier.child_by_field_name("alias")
                local_name = exports.get(_text(original, source)) if original is not None else None
                if local_name is not None:
                    symbols[_text(alias or original, source)] = f"{imported.stem}.{local_name}"
    return symbols
