"""Resolve proven local Node exports for route handlers and instance calls."""

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


def _anonymous_default_callable(statement: Node) -> Node | None:
    if statement.type != "export_statement" or not any(child.type == "default" for child in statement.children):
        return None
    declaration = statement.child_by_field_name("declaration")
    if declaration is not None and declaration.type == "function_declaration":
        if declaration.child_by_field_name("name") is None and declaration.child_by_field_name("body") is not None:
            return declaration
    for child in statement.named_children:
        if child.type in _FUNCTION_VALUES and child.child_by_field_name("body") is not None:
            return child
    return None


def anonymous_default_function(tree: Node) -> Node | None:
    """Return an anonymous callable exported directly as default, if present."""
    return next((value for statement in tree.named_children if (value := _anonymous_default_callable(statement))), None)


def _local_functions(tree: Node, source: bytes) -> set[str]:
    names: set[str] = set()
    for statement in tree.named_children:
        declaration = statement.child_by_field_name("declaration") if statement.type == "export_statement" else statement
        if declaration is not None:
            names.update(_declared_functions(declaration, source))
    return names


def _exported_functions(path: Path) -> dict[str, str]:
    source = path.read_bytes()
    tree = _parse(path, source)
    local_functions = _local_functions(tree, source)

    exports: dict[str, str] = {}
    for statement in tree.named_children:
        if statement.type != "export_statement":
            continue
        is_default = any(child.type == "default" for child in statement.children)
        if is_default:
            local_name: str | None = None
            declaration = statement.child_by_field_name("declaration")
            if declaration is not None:
                names = _declared_functions(declaration, source)
                if len(names) == 1:
                    local_name = next(iter(names))
            else:
                identifier = next((child for child in statement.named_children if child.type == "identifier"), None)
                if identifier is not None and _text(identifier, source) in local_functions:
                    local_name = _text(identifier, source)
            if local_name is None and _anonymous_default_callable(statement) is not None:
                local_name = "default"
            if local_name is not None:
                exports["default"] = local_name
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


def _commonjs_assignment_values(tree: Node, source: bytes) -> list[Node | None]:
    exports: list[Node | None] = []
    for statement in tree.named_children:
        if statement.type != "expression_statement" or not statement.named_children:
            continue
        assignment = statement.named_children[0]
        if assignment.type != "assignment_expression":
            continue
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if left is None or not _is_module_exports(left, source):
            continue
        exports.append(right)
    return exports


def _is_module_exports(node: Node, source: bytes) -> bool:
    if node.type != "member_expression":
        return False
    receiver = node.child_by_field_name("object")
    property_name = node.child_by_field_name("property")
    return (
        receiver is not None and receiver.type == "identifier" and _text(receiver, source) == "module"
        and property_name is not None and _text(property_name, source) == "exports"
    )


def _is_exports_property(node: Node, source: bytes, aliases: set[str] | None = None) -> bool:
    if node.type not in {"member_expression", "subscript_expression"}:
        return False
    receiver = node.child_by_field_name("object")
    return receiver is not None and _is_exports_object(receiver, source, aliases)


def _is_exports_object(node: Node, source: bytes, aliases: set[str] | None = None) -> bool:
    if _is_module_exports(node, source):
        return True
    if node.type != "identifier":
        return False
    name = _text(node, source)
    return name == "exports" or (aliases is not None and name in aliases)


def _top_level_declarators(tree: Node, *, include_exports: bool = False) -> list[Node]:
    declarators = []
    for statement in tree.named_children:
        declaration = (
            statement.child_by_field_name("declaration")
            if include_exports and statement.type == "export_statement"
            else statement
        )
        if declaration is not None and declaration.type in {"lexical_declaration", "variable_declaration"}:
            declarators.extend(
                child for child in declaration.named_children if child.type == "variable_declarator"
            )
    return declarators


def _commonjs_export_aliases(declarations: list[Node], source: bytes) -> set[str]:
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for declaration in declarations:
            name = declaration.child_by_field_name("name")
            value = declaration.child_by_field_name("value")
            if name is None or name.type != "identifier" or value is None:
                continue
            alias = _text(name, source)
            if alias not in aliases and _is_exports_object(value, source, aliases):
                aliases.add(alias)
                changed = True
    return aliases


def _contains_export_reference(node: Node, source: bytes, aliases: set[str], containers: set[str]) -> bool:
    if _is_exports_object(node, source, aliases):
        return True
    if node.type == "identifier":
        return _text(node, source) in containers
    if node.type == "shorthand_property_identifier":
        name = _text(node, source)
        return name == "exports" or name in aliases or name in containers
    if node.type == "pair":
        value = node.child_by_field_name("value")
        return value is not None and _contains_export_reference(value, source, aliases, containers)
    if node.type not in {"object", "array", "parenthesized_expression", "spread_element"}:
        return False
    return any(_contains_export_reference(child, source, aliases, containers) for child in node.named_children)


def _commonjs_export_containers(declarations: list[Node], source: bytes, aliases: set[str]) -> set[str]:
    containers: set[str] = set()
    changed = True
    while changed:
        changed = False
        for declaration in declarations:
            name = declaration.child_by_field_name("name")
            value = declaration.child_by_field_name("value")
            if name is None or name.type != "identifier" or value is None:
                continue
            container = _text(name, source)
            if container not in aliases and container not in containers:
                if not _contains_export_reference(value, source, aliases, containers):
                    continue
                containers.add(container)
                changed = True
    return containers


def _has_commonjs_export_mutation_or_escape(tree: Node, source: bytes) -> bool:
    declarations = _top_level_declarators(tree)
    aliases = _commonjs_export_aliases(declarations, source)
    containers = _commonjs_export_containers(declarations, source, aliases)
    pending = [tree]
    while pending:
        node = pending.pop()
        pending.extend(node.named_children)
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left is not None and left.type in {"member_expression", "subscript_expression"}:
                receiver = left.child_by_field_name("object")
                if receiver is not None and receiver.type == "identifier" and _text(receiver, source) in aliases:
                    return True
        elif node.type in {"unary_expression", "update_expression"}:
            argument = node.child_by_field_name("argument")
            is_delete = node.type == "unary_expression" and node.children[0].type == "delete"
            if (is_delete or node.type == "update_expression") and argument is not None:
                if _is_exports_property(argument, source, aliases):
                    return True
        elif node.type == "return_statement":
            if any(_contains_export_reference(child, source, aliases, containers) for child in node.named_children):
                return True
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is None or arguments is None:
                continue
            exported_args = [
                index for index, argument in enumerate(arguments.named_children)
                if _is_exports_object(argument, source, aliases)
            ]
            if exported_args and (_text(function, source) != "Object.assign" or 0 in exported_args):
                return True
            if any(
                _contains_export_reference(argument, source, aliases, containers)
                and not _is_exports_object(argument, source, aliases)
                for argument in arguments.named_children
            ):
                return True
    return False


def _commonjs_property_assignments(tree: Node, source: bytes) -> list[tuple[Node, bool]]:
    assignments: list[tuple[Node, bool]] = []
    pending = [tree]
    while pending:
        node = pending.pop()
        pending.extend(node.named_children)
        if node.type != "assignment_expression":
            continue
        left = node.child_by_field_name("left")
        if left is not None and _is_exports_property(left, source):
            top_level = node.parent is not None and node.parent.type == "expression_statement" and node.parent.parent == tree
            assignments.append((node, top_level))
    return assignments


def _has_exports_rebinding(tree: Node, source: bytes) -> bool:
    pending = [tree]
    while pending:
        node = pending.pop()
        pending.extend(node.named_children)
        if node.type not in {"assignment_expression", "variable_declarator"}:
            continue
        name = node.child_by_field_name("left" if node.type == "assignment_expression" else "name")
        if name is not None and name.type == "identifier" and _text(name, source) == "exports":
            return True
    return False


def anonymous_commonjs_function(tree: Node, source: bytes) -> Node | None:
    """Return a callable assigned directly and uniquely to ``module.exports``."""
    values = _commonjs_assignment_values(tree, source)
    if len(values) != 1:
        return None
    value = values[0]
    return value if value is not None and value.type in _FUNCTION_VALUES and value.child_by_field_name("body") else None


def _commonjs_exported_function(path: Path) -> str | None:
    source = path.read_bytes()
    tree = _parse(path, source)
    values = _commonjs_assignment_values(tree, source)
    if len(values) != 1 or values[0] is None:
        return None
    value = values[0]
    if value.type == "identifier" and _text(value, source) in _local_functions(tree, source):
        return _text(value, source)
    return "exports" if anonymous_commonjs_function(tree, source) is not None else None


def _assigned_commonjs_named_exports(
    assignments: list[tuple[Node, bool]], local_functions: set[str], source: bytes,
) -> dict[str, str]:
    exports: dict[str, str] = {}
    seen: set[str] = set()
    for assignment, top_level in assignments:
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if not top_level or left is None or left.type != "member_expression":
            return {}
        property_name = left.child_by_field_name("property")
        if property_name is None or property_name.type != "property_identifier":
            return {}
        exported_name = _text(property_name, source)
        if exported_name in seen:
            return {}
        seen.add(exported_name)
        if right is not None and right.type == "identifier" and _text(right, source) in local_functions:
            exports[exported_name] = _text(right, source)
    return exports


def _commonjs_named_exports(path: Path) -> dict[str, str]:
    source = path.read_bytes()
    tree = _parse(path, source)
    if _has_commonjs_export_mutation_or_escape(tree, source):
        return {}
    values = _commonjs_assignment_values(tree, source)
    local_functions = _local_functions(tree, source)
    assignments = _commonjs_property_assignments(tree, source)
    if assignments:
        if values or _has_exports_rebinding(tree, source):
            return {}
        return _assigned_commonjs_named_exports(assignments, local_functions, source)
    if len(values) != 1 or values[0] is None or values[0].type != "object":
        return {}
    exports: dict[str, str] = {}
    seen: set[str] = set()
    for property_node in values[0].named_children:
        if property_node.type == "shorthand_property_identifier":
            exported_name = local_name = _text(property_node, source)
        elif property_node.type == "pair":
            key = property_node.child_by_field_name("key")
            value = property_node.child_by_field_name("value")
            if key is None or key.type not in {"property_identifier", "string"}:
                return {}
            exported_name = _text(key, source).strip("\"'")
            local_name = _text(value, source) if value is not None and value.type == "identifier" else ""
        else:
            return {}
        if exported_name in seen:
            return {}
        seen.add(exported_name)
        if local_name in local_functions:
            exports[exported_name] = local_name
    return exports


def _commonjs_destructured_handlers(name: Node, exports: dict[str, str], source: bytes) -> dict[str, str]:
    handlers: dict[str, str] = {}
    for property_node in name.named_children:
        if property_node.type == "shorthand_property_identifier_pattern":
            exported_name = local_name = _text(property_node, source)
        elif property_node.type == "pair_pattern":
            key = property_node.child_by_field_name("key")
            value = property_node.child_by_field_name("value")
            if key is None or key.type != "property_identifier" or value is None or value.type != "identifier":
                continue
            exported_name = _text(key, source)
            local_name = _text(value, source)
        else:
            continue
        if exported_name in exports:
            handlers[local_name] = exports[exported_name]
    return handlers


def _relative_commonjs_requires(tree: Node, source: bytes, path: Path, root: Path) -> list[tuple[Node, Path]]:
    imports: list[tuple[Node, Path]] = []
    for statement in tree.named_children:
        if statement.type not in {"lexical_declaration", "variable_declaration"}:
            continue
        for variable in statement.named_children:
            if variable.type != "variable_declarator":
                continue
            name = variable.child_by_field_name("name")
            value = variable.child_by_field_name("value")
            if name is None or name.type not in {"identifier", "object_pattern"}:
                continue
            if value is None or value.type != "call_expression":
                continue
            function = value.child_by_field_name("function")
            arguments = value.child_by_field_name("arguments")
            if function is None or _text(function, source) != "require" or arguments is None:
                continue
            args = arguments.named_children
            if len(args) != 1 or args[0].type != "string":
                continue
            module = _text(args[0], source)[1:-1]
            imported = resolve_local_source(path, module, root, suffixes=_SOURCE_SUFFIXES)
            if imported is not None:
                imports.append((name, imported))
    return imports


def _commonjs_handler_imports(tree: Node, source: bytes, path: Path, root: Path) -> dict[str, str]:
    symbols: dict[str, str] = {}
    for name, imported in _relative_commonjs_requires(tree, source, path, root):
        if name.type == "identifier":
            handler = _commonjs_exported_function(imported)
            if handler is not None:
                symbols[_text(name, source)] = f"{imported.stem}.{handler}"
        else:
            exports = _commonjs_named_exports(imported)
            for local_name, handler in _commonjs_destructured_handlers(name, exports, source).items():
                symbols[local_name] = f"{imported.stem}.{handler}"
    return symbols


def _commonjs_exported_instance(path: Path) -> str | None:
    source = path.read_bytes()
    tree = _parse(path, source)
    values = _commonjs_assignment_values(tree, source)
    if len(values) != 1 or values[0] is None or values[0].type != "new_expression":
        return None
    if _commonjs_property_assignments(tree, source) or _has_commonjs_export_mutation_or_escape(tree, source):
        return None
    constructor = values[0].child_by_field_name("constructor")
    if constructor is None or constructor.type != "identifier":
        return None
    class_name = _text(constructor, source)
    classes = [
        statement for statement in tree.named_children
        if statement.type == "class_declaration"
        and (name := statement.child_by_field_name("name")) is not None
        and _text(name, source) == class_name
    ]
    return class_name if len(classes) == 1 else None


def _stable_const_binding(tree: Node, source: bytes, name: str) -> Node | None:
    """Find one top-level const binding that is never reassigned or shadowed."""
    bindings: list[Node] = []
    pending = [tree]
    while pending:
        node = pending.pop()
        pending.extend(node.named_children)
        if node.type == "variable_declarator":
            identifier = node.child_by_field_name("name")
            if identifier is not None and identifier.type == "identifier" and _text(identifier, source) == name:
                bindings.append(node)
        elif node.type == "formal_parameters":
            if any(child.type == "identifier" and _text(child, source) == name for child in node.named_children):
                return None
        elif node.type in {"assignment_expression", "augmented_assignment_expression", "update_expression"}:
            left = node.child_by_field_name("left") or node.child_by_field_name("argument")
            if left is not None and left.type == "identifier" and _text(left, source) == name:
                return None
    if len(bindings) != 1:
        return None
    declaration = bindings[0].parent
    if declaration is None or declaration.type != "lexical_declaration":
        return None
    parent = declaration.parent
    if parent != tree and not (
        parent is not None and parent.type == "export_statement" and parent.parent == tree
    ):
        return None
    if not _text(declaration, source).lstrip().startswith("const "):
        return None
    return bindings[0].child_by_field_name("value")


def _mongoose_factory_bindings(tree: Node, source: bytes) -> set[str]:
    factories: set[str] = set()
    for variable in _top_level_declarators(tree, include_exports=True):
        name = variable.child_by_field_name("name")
        if name is None or name.type != "identifier":
            continue
        alias = _text(name, source)
        value = _stable_const_binding(tree, source, alias)
        if value is None or value.type != "call_expression":
            continue
        function = value.child_by_field_name("function")
        arguments = value.child_by_field_name("arguments")
        if (
            function is not None and _text(function, source) == "require"
            and arguments is not None and len(arguments.named_children) == 1
            and arguments.named_children[0].type == "string"
            and _text(arguments.named_children[0], source)[1:-1] == "mongoose"
        ):
            factories.add(alias)
    for statement in tree.named_children:
        if statement.type != "import_statement":
            continue
        module = statement.child_by_field_name("source")
        if module is None or _text(module, source)[1:-1] != "mongoose":
            continue
        clause = next((child for child in statement.named_children if child.type == "import_clause"), None)
        if clause is None:
            continue
        for imported in clause.named_children:
            alias_node = (
                imported if imported.type == "identifier"
                else imported.named_children[0] if imported.type == "namespace_import" and imported.named_children
                else None
            )
            if alias_node is None:
                continue
            alias = _text(alias_node, source)
            if _unshadowed_import_alias(tree, source, alias):
                factories.add(alias)
    return factories


def _walk_nodes(node: Node):
    yield node
    for child in node.named_children:
        yield from _walk_nodes(child)


def _mongoose_model_call(call: Node, source: bytes, factories: set[str]) -> tuple[str, str | None] | None:
    if call.type != "call_expression":
        return None
    function = call.child_by_field_name("function")
    arguments = call.child_by_field_name("arguments")
    if function is None or function.type != "member_expression" or arguments is None:
        return None
    receiver = function.child_by_field_name("object")
    method = function.child_by_field_name("property")
    if receiver is None or receiver.type != "identifier" or method is None:
        return None
    if _text(receiver, source) not in factories or _text(method, source) != "model":
        return None
    args = arguments.named_children
    if len(args) < 2 or args[0].type != "string":
        return None
    model_name = _text(args[0], source)[1:-1]
    if not model_name or "\\" in model_name:
        return None
    collection = None
    if len(args) >= 3 and args[2].type == "string":
        literal = _text(args[2], source)[1:-1]
        if literal and "\\" not in literal:
            collection = literal
    return model_name, collection


def proven_local_mongoose_model_declarations(tree: Node, source: bytes) -> dict[str, tuple[str, str | None, int]]:
    """Map stable local model aliases to literal model names and source lines."""
    factories = _mongoose_factory_bindings(tree, source)
    models: dict[str, tuple[str, str | None, int]] = {}
    for variable in _top_level_declarators(tree, include_exports=True):
        name = variable.child_by_field_name("name")
        if name is None or name.type != "identifier":
            continue
        alias = _text(name, source)
        value = _stable_const_binding(tree, source, alias)
        if value is None:
            continue
        model = _mongoose_model_call(value, source, factories)
        if model is not None:
            models[alias] = (*model, value.start_point.row + 1)
    return models


def local_mongoose_model_declarations(path: Path) -> dict[str, tuple[str, str | None, int]]:
    """Read one JS/TS module's proven local Mongoose model declarations."""
    source = path.read_bytes()
    return proven_local_mongoose_model_declarations(_parse(path, source), source)


def _default_mongoose_model_export(tree: Node, source: bytes) -> tuple[str, str | None, int] | None:
    defaults = [
        statement for statement in tree.named_children
        if statement.type == "export_statement" and any(child.type == "default" for child in statement.children)
    ]
    if len(defaults) != 1:
        return None
    value = defaults[0].child_by_field_name("value")
    if value is None:
        return None
    if value.type == "identifier":
        return proven_local_mongoose_model_declarations(tree, source).get(_text(value, source))
    model = _mongoose_model_call(value, source, _mongoose_factory_bindings(tree, source))
    return (*model, value.start_point.row + 1) if model is not None else None


def proven_default_mongoose_model_export(path: Path) -> tuple[str, str | None, int] | None:
    """Return a directly default-exported, source-proven Mongoose model."""
    source = path.read_bytes()
    return _default_mongoose_model_export(_parse(path, source), source)


def _exported_mongoose_models(path: Path) -> dict[str, str]:
    source = path.read_bytes()
    tree = _parse(path, source)
    models = proven_local_mongoose_model_declarations(tree, source)
    exported: dict[str, str] = {}
    if (default := _default_mongoose_model_export(tree, source)) is not None:
        exported["default"] = default[0]
    for statement in tree.named_children:
        if statement.type != "export_statement":
            continue
        declaration = statement.child_by_field_name("declaration")
        if declaration is None or declaration.type != "lexical_declaration":
            continue
        for variable in declaration.named_children:
            if variable.type != "variable_declarator":
                continue
            name = variable.child_by_field_name("name")
            if name is not None and (model := models.get(_text(name, source))) is not None:
                exported[_text(name, source)] = model[0]
    return exported


def _unshadowed_import_alias(tree: Node, source: bytes, alias: str) -> bool:
    for node in _walk_nodes(tree):
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is not None and _text(name, source) == alias:
                return False
        elif node.type == "formal_parameters":
            if any(child.type == "identifier" and _text(child, source) == alias for child in _walk_nodes(node)):
                return False
        elif node.type in {"assignment_expression", "augmented_assignment_expression", "update_expression"}:
            left = node.child_by_field_name("left") or node.child_by_field_name("argument")
            if left is not None and left.type == "identifier" and _text(left, source) == alias:
                return False
    return True


def proven_local_esm_mongoose_models(tree: Node, source: bytes, path: Path, root: Path) -> dict[str, str]:
    """Resolve direct named/default imports of source-proven local Mongoose models."""
    candidates: list[tuple[str, str]] = []
    for statement in tree.named_children:
        if statement.type != "import_statement" or any(child.type == "type" for child in statement.children):
            continue
        module_node = statement.child_by_field_name("source")
        clause = next((child for child in statement.named_children if child.type == "import_clause"), None)
        if module_node is None or clause is None:
            continue
        imported = resolve_local_source(path, _text(module_node, source)[1:-1], root, suffixes=_SOURCE_SUFFIXES)
        if imported is None:
            continue
        exports = _exported_mongoose_models(imported)
        for names in clause.named_children:
            if names.type == "identifier":
                if (model := exports.get("default")) is not None:
                    candidates.append((_text(names, source), model))
                continue
            if names.type != "named_imports":
                continue
            for specifier in names.named_children:
                if specifier.type != "import_specifier" or any(child.type == "type" for child in specifier.children):
                    continue
                original = specifier.child_by_field_name("name")
                alias = specifier.child_by_field_name("alias")
                if original is None or (model := exports.get(_text(original, source))) is None:
                    continue
                candidates.append((_text(alias or original, source), model))
    return {
        alias: model for alias, model in candidates
        if sum(name == alias for name, _ in candidates) == 1 and _unshadowed_import_alias(tree, source, alias)
    }


def proven_commonjs_mongoose_model_export(path: Path) -> tuple[str, str | None, int] | None:
    """Return the literal model name and source line of a direct CommonJS export."""
    source = path.read_bytes()
    tree = _parse(path, source)
    values = _commonjs_assignment_values(tree, source)
    if len(values) != 1 or values[0] is None or values[0].type != "call_expression":
        return None
    if _commonjs_property_assignments(tree, source) or _has_commonjs_export_mutation_or_escape(tree, source):
        return None
    call = values[0]
    function = call.child_by_field_name("function")
    arguments = call.child_by_field_name("arguments")
    if function is None or function.type != "member_expression" or arguments is None:
        return None
    model = _mongoose_model_call(call, source, _mongoose_factory_bindings(tree, source))
    return (*model, call.start_point.row + 1) if model is not None else None


def proven_local_commonjs_mongoose_models(tree: Node, source: bytes, path: Path, root: Path) -> dict[str, str]:
    """Resolve immutable require aliases to the exported Mongoose model names."""
    models: dict[str, str] = {}
    for name, imported in _relative_commonjs_requires(tree, source, path, root):
        if name.type != "identifier":
            continue
        alias = _text(name, source)
        model = proven_commonjs_mongoose_model_export(imported)
        if _stable_const_binding(tree, source, alias) is not None and model is not None:
            models[alias] = model[0]
    return models


def proven_local_commonjs_flow_imports(tree: Node, source: bytes, path: Path, root: Path) -> tuple[tuple[str, str], ...]:
    """Bind relative require aliases to proven local class methods or object functions."""
    imports: list[tuple[str, str]] = []
    for name, imported in _relative_commonjs_requires(tree, source, path, root):
        if name.type == "object_pattern":
            declaration = name.parent.parent if name.parent is not None else None
            if declaration is None or declaration.type != "lexical_declaration" or not _text(
                declaration, source
            ).lstrip().startswith("const "):
                continue
            exports = _commonjs_named_exports(imported)
            for local_name, function_name in _commonjs_destructured_handlers(name, exports, source).items():
                imports.append((local_name, f"{imported.stem}.{function_name}"))
            continue
        if name.type != "identifier":
            continue
        alias = _text(name, source)
        class_name = _commonjs_exported_instance(imported)
        if class_name is not None:
            imports.append((alias, class_name))
            continue
        for exported_name, local_name in _commonjs_named_exports(imported).items():
            imports.append((f"{alias}.{exported_name}", f"{imported.stem}.{local_name}"))
    return tuple(imports)


def proven_local_handler_imports(tree: Node, source: bytes, path: Path, root: Path) -> dict[str, str]:
    """Map local aliases to analyzed symbols only after a relative import/export proof."""
    root = root.resolve()
    symbols = _commonjs_handler_imports(tree, source, path, root)
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
