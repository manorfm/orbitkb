"""Source-proven Nest controller and method decorator routes."""

from __future__ import annotations

from pathlib import Path

import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from orbitkb.discovery.node_imports import parse_node_named_import_declarations

_HTTP_DECORATORS = {
    "Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH", "Delete": "DELETE",
}


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _string(node: Node, source: bytes) -> str | None:
    value = _text(node, source).strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    return None


def nest_imports(source: str) -> dict[str, str]:
    """Return local names for decorators imported from exactly @nestjs/common."""
    return {
        local: original
        for local, module, original in parse_node_named_import_declarations(source)
        if module == "@nestjs/common"
    }


def preceding_decorators(node: Node) -> list[Node]:
    parent = node.parent
    if parent is None:
        return []
    siblings = parent.named_children
    index = next((i for i, child in enumerate(siblings) if child.start_byte == node.start_byte), None)
    if index is None:
        return []
    decorators: list[Node] = []
    for sibling in reversed(siblings[:index]):
        if sibling.type != "decorator":
            break
        decorators.append(sibling)
    return list(reversed(decorators))


def nest_decorator_path(
    decorators: list[Node], source: bytes, imported_names: dict[str, str], expected: str,
) -> str | None:
    for decorator in decorators:
        call = next((child for child in decorator.named_children if child.type == "call_expression"), None)
        if call is None:
            continue
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if function is None or arguments is None or imported_names.get(_text(function, source)) != expected:
            continue
        args = arguments.named_children
        if not args:
            return ""
        return _string(args[0], source) if len(args) == 1 else None
    return None


def nest_method_route(
    decorators: list[Node], source: bytes, imported_names: dict[str, str],
) -> tuple[str, str] | None:
    for decorator_name, method in _HTTP_DECORATORS.items():
        path = nest_decorator_path(decorators, source, imported_names, decorator_name)
        if path is not None:
            return method, path
    return None


def find_nest_route_hints(path: Path) -> list[tuple[str, str, int]]:
    """Find HTTP handlers inside a locally imported Nest controller class."""
    source = path.read_bytes()
    imports = nest_imports(source.decode("utf-8", errors="ignore"))
    if "Controller" not in imports.values():
        return []
    tree = Parser(Language(tree_sitter_typescript.language_typescript())).parse(source).root_node
    routes: list[tuple[str, str, int]] = []

    def walk(node: Node):
        yield node
        for child in node.named_children:
            yield from walk(child)

    for class_node in walk(tree):
        if class_node.type != "class_declaration":
            continue
        prefix = nest_decorator_path(preceding_decorators(class_node), source, imports, "Controller")
        class_body = class_node.child_by_field_name("body")
        if prefix is None or class_body is None:
            continue
        decorators: list[Node] = []
        for child in class_body.named_children:
            if child.type == "decorator":
                decorators.append(child)
                continue
            if child.type != "method_definition":
                decorators.clear()
                continue
            route_decorators = list(decorators)
            decorators.clear()
            route = nest_method_route(route_decorators, source, imports)
            if route is None or child.child_by_field_name("name") is None or child.child_by_field_name("body") is None:
                continue
            method, local_path = route
            full_path = f"{prefix.rstrip('/')}/{local_path.lstrip('/')}" if local_path else prefix.rstrip("/") or "/"
            routes.append((method, full_path, route_decorators[0].start_point.row + 1))
    return routes
