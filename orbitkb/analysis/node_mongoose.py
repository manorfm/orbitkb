"""Prove local Mongoose document receivers for instance persistence calls."""

from __future__ import annotations

from tree_sitter import Node


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _walk(node: Node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _document_query(expression: Node, source: bytes, models: frozenset[str]) -> bool:
    if expression.type in {"await_expression", "parenthesized_expression"}:
        return len(expression.named_children) == 1 and _document_query(expression.named_children[0], source, models)
    if expression.type != "call_expression":
        return False
    function = expression.child_by_field_name("function")
    if function is None or function.type != "member_expression":
        return False
    receiver = function.child_by_field_name("object")
    method = function.child_by_field_name("property")
    if receiver is None or method is None:
        return False
    name = _text(method, source)
    if receiver.type == "identifier":
        return _text(receiver, source) in models and name in {"findOne", "findById"}
    return name == "sort" and _document_query(receiver, source, models)


def proven_mongoose_documents(
    declaration: Node, body: Node, source: bytes, models: frozenset[str],
) -> frozenset[str]:
    """Return immutable local names initialized by a document-producing query."""
    candidates: set[str] = set()
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
                and value is not None and _document_query(value, source, models)
            ):
                candidates.add(alias)
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
    return frozenset(
        alias for alias in candidates
        if declarations.get(alias) == 1 and alias not in invalid
    )
