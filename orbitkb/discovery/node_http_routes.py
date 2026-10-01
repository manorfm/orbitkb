"""Literal Node HTTP route calls and shared Fastify object parsing."""

from __future__ import annotations

from pathlib import Path

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_DIRECT_METHODS = frozenset(method.lower() for method in _HTTP_METHODS)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _string(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
    value = _text(node, source).strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    return None


def literal_fastify_route_definition(args: list[Node], source: bytes) -> tuple[tuple[str, ...], str, Node] | None:
    """Accept one object with unique literal method/url fields and a handler."""
    if len(args) != 1 or args[0].type != "object":
        return None
    fields: dict[str, Node] = {}
    for pair in args[0].named_children:
        if pair.type != "pair":
            continue
        key = pair.child_by_field_name("key")
        value = pair.child_by_field_name("value")
        if key is None or value is None:
            continue
        field_name = _text(key, source)
        if field_name not in {"method", "url", "handler"}:
            continue
        if field_name in fields:
            return None
        fields[field_name] = value
    method_node = fields.get("method")
    if method_node is None:
        return None
    method_nodes = method_node.named_children if method_node.type == "array" else (method_node,)
    if not method_nodes:
        return None
    methods = tuple(_string(node, source) for node in method_nodes)
    if any(method is None for method in methods):
        return None
    normalized = tuple(method.upper() for method in methods if method is not None)
    if len(set(normalized)) != len(normalized) or any(method not in _HTTP_METHODS for method in normalized):
        return None
    path = _string(fields.get("url"), source)
    handler = fields.get("handler")
    if path is None or handler is None:
        return None
    return normalized, path, handler


def _walk(node: Node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _local_handlers(tree: Node, source: bytes) -> frozenset[str]:
    names: set[str] = set()
    for node in _walk(tree):
        if node.type == "function_declaration":
            name = node.child_by_field_name("name")
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is None or value.type != "arrow_function":
                continue
            name = node.child_by_field_name("name")
        else:
            continue
        if name is not None:
            names.add(_text(name, source))
    return frozenset(names)


def _local_handler(handler: Node, handlers: frozenset[str], source: bytes) -> bool:
    if handler.type == "identifier":
        return _text(handler, source) in handlers
    return handler.type in {"arrow_function", "function_expression"} and handler.child_by_field_name("body") is not None


def find_literal_node_routes(
    path: Path, direct_receivers: frozenset[str], fastify_receivers: frozenset[str],
) -> list[tuple[str, str, str, int]]:
    """Find direct routes and Fastify objects with literal paths and local handlers."""
    if not direct_receivers and not fastify_receivers:
        return []
    source = path.read_bytes()
    grammar = (
        tree_sitter_typescript.language_typescript() if path.suffix == ".ts"
        else tree_sitter_javascript.language()
    )
    tree = Parser(Language(grammar)).parse(source).root_node
    handlers = _local_handlers(tree, source)
    routes: list[tuple[str, str, str, int]] = []
    for node in _walk(tree):
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if callee is None or args is None or callee.type != "member_expression":
            continue
        receiver = callee.child_by_field_name("object")
        method = callee.child_by_field_name("property")
        if receiver is None or method is None:
            continue
        receiver_name = _text(receiver, source)
        method_name = _text(method, source)
        arguments = args.named_children
        if receiver_name in direct_receivers and method_name in _DIRECT_METHODS:
            route = _string(arguments[0], source) if arguments else None
            handler = arguments[-1] if len(arguments) > 1 else None
            if route is not None and handler is not None and _local_handler(handler, handlers, source):
                routes.append((receiver_name, method_name.upper(), route, node.start_point.row + 1))
            continue
        if receiver_name not in fastify_receivers or method_name != "route":
            continue
        definition = literal_fastify_route_definition(arguments, source)
        if definition is None:
            continue
        methods, route, handler = definition
        if not _local_handler(handler, handlers, source):
            continue
        routes.extend((receiver_name, http_method, route, node.start_point.row + 1) for http_method in methods)
    return routes
