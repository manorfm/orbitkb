"""Prove local Mongoose document receivers for instance persistence calls."""

from __future__ import annotations

from collections.abc import Mapping

from tree_sitter import Node


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _walk(node: Node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _document_query(expression: Node, source: bytes, models: Mapping[str, str | None]) -> str | None:
    if expression.type in {"await_expression", "parenthesized_expression"}:
        if len(expression.named_children) != 1:
            return None
        return _document_query(expression.named_children[0], source, models)
    if expression.type != "call_expression":
        return None
    function = expression.child_by_field_name("function")
    if function is None or function.type != "member_expression":
        return None
    receiver = function.child_by_field_name("object")
    method = function.child_by_field_name("property")
    if receiver is None or method is None:
        return None
    name = _text(method, source)
    if receiver.type == "identifier":
        alias = _text(receiver, source)
        return alias if alias in models and name in {"findOne", "findById"} else None
    return _document_query(receiver, source, models) if name == "sort" else None


def is_mongoose_query_sort(call: Node, source: bytes, models: Mapping[str, str | None]) -> bool:
    """Identify a sort modifier on a proven single-document Mongoose query."""
    if call.type != "call_expression":
        return False
    function = call.child_by_field_name("function")
    if function is None or function.type != "member_expression":
        return False
    receiver = function.child_by_field_name("object")
    method = function.child_by_field_name("property")
    return (
        receiver is not None and method is not None
        and _text(method, source) == "sort"
        and _document_query(receiver, source, models) is not None
    )


def proven_mongoose_documents(
    declaration: Node, body: Node, source: bytes, models: Mapping[str, str | None],
) -> dict[str, str]:
    """Map immutable document locals to their proven model receiver aliases."""
    candidates: dict[str, str] = {}
    declarations: dict[str, int] = {}
    invalid: set[str] = set()
    for node in _walk(body):
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is None or name.type != "identifier":
                continue
            alias = _text(name, source)
            declarations[alias] = declarations.get(alias, 0) + 1
            parent = node.parent
            value = node.child_by_field_name("value")
            if (
                parent is not None and parent.type == "lexical_declaration"
                and parent.parent == body
                and _text(parent, source).lstrip().startswith("const ")
                and value is not None and (model_alias := _document_query(value, source, models)) is not None
            ):
                candidates[alias] = model_alias
        elif node.type in {"assignment_expression", "augmented_assignment_expression", "update_expression"}:
            left = node.child_by_field_name("left") or node.child_by_field_name("argument")
            if left is None:
                continue
            if left.type == "identifier":
                invalid.add(_text(left, source))
            elif left.type in {"member_expression", "subscript_expression"}:
                receiver = left.child_by_field_name("object")
                if receiver is not None and receiver.type == "identifier" and _text(left, source).endswith(".save"):
                    invalid.add(_text(receiver, source))
    for node in _walk(declaration):
        if node.type != "formal_parameters":
            continue
        invalid.update(_text(parameter, source) for parameter in node.named_children if parameter.type == "identifier")
    return {
        alias: model_alias for alias, model_alias in candidates.items()
        if declarations.get(alias) == 1 and alias not in invalid
    }
