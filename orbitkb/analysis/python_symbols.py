"""Stable identities for Python declarations inside an indexed source root."""
from __future__ import annotations

import ast
from pathlib import Path


def module_name(path: Path, root: Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)


def imported_module_path(file: Path, root: Path, module: str, level: int) -> Path | None:
    """Resolve one local Python module without leaving the indexed root."""
    root = root.resolve()
    if level:
        package = file.resolve().parent
        for depth in range(level):
            if not (package / "__init__.py").is_file():
                return None
            if depth + 1 < level:
                package = package.parent
        base = package
    else:
        base = root
    source = base.joinpath(*module.split(".")).with_suffix(".py") if module else base / "__init__.py"
    source = source.resolve()
    return source if source.is_relative_to(root) and source.is_file() else None


class _Bindings(ast.NodeVisitor):
    """Names bound in one scope; nested function/class bodies have their own scope."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)


def _bound_names(node: ast.AST) -> set[str]:
    collector = _Bindings()
    collector.visit(node)
    return collector.names


def stable_local_imports(tree: ast.Module, file: Path, root: Path) -> dict[str, str]:
    """Keep direct local imports whose bound names are never reassigned."""
    imports: dict[str, str] = {}
    bindings: dict[str, str] = {}

    def invalidate(name: str) -> None:
        imports.pop(name, None)
        for key in tuple(imports):
            if key.startswith(f"{name}."):
                imports.pop(key)
        bindings[name] = "invalid"

    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                name = alias.asname or alias.name.split(".", 1)[0]
                dotted = alias.asname is None and "." in alias.name
                source = imported_module_path(file, root, alias.name, 0)
                if source is None:
                    invalidate(name)
                    continue
                current = bindings.get(name)
                if current is not None and not (current == "dotted" and dotted):
                    invalidate(name)
                    continue
                if current == "invalid":
                    continue
                bindings[name] = "dotted" if dotted else "direct"
                imports[alias.name if dotted else name] = module_name(source, root)
            continue
        candidates: dict[str, str] = {}
        if isinstance(statement, ast.ImportFrom) and statement.module:
            source = imported_module_path(file, root, statement.module, statement.level)
            if source is not None:
                module = module_name(source, root)
                candidates = {alias.asname or alias.name: f"{module}.{alias.name}"
                              for alias in statement.names if alias.name != "*"}
        for name in _bound_names(statement):
            if name in bindings:
                invalidate(name)
                continue
            bindings[name] = "direct"
            candidate = candidates.get(name)
            if candidate is not None:
                imports[name] = candidate
    return imports


def module_import_names(tree: ast.Module) -> set[str]:
    """Names introduced by imports, including conditional imports at module scope."""
    names: set[str] = set()

    class Collector(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> None:
            names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            pass

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            pass

    collector = Collector()
    for statement in tree.body:
        collector.visit(statement)
    return names


def local_bindings(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters and assignments that shadow a module import inside a callable."""
    names = {argument.arg for argument in (
        *function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs,
    )}
    names.update(argument.arg for argument in (function.args.vararg, function.args.kwarg) if argument is not None)
    for statement in function.body:
        names.update(_bound_names(statement))
    return names
