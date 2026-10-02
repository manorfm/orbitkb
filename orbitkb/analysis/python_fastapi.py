"""Source-proven FastAPI routes, including literal APIRouter mounts."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from orbitkb.analysis.python_symbols import (
    imported_module_path,
    imported_submodule_path,
    module_name,
)

METHODS = {name: name.upper() for name in ("get", "post", "put", "patch", "delete", "head", "options")}


@dataclass(frozen=True)
class Route:
    file: Path
    line: int
    method: str
    path: str
    symbol: str
    router: str | None = None


@dataclass(frozen=True)
class ImportBinding:
    module: str
    name: str
    level: int
    module_import: bool = False


@dataclass(frozen=True)
class Mount:
    owner: str
    router: str
    prefix: str
    line: int
    imported_from: ImportBinding | None
    on_app: bool
    router_attribute: str | None = None


@dataclass
class ModuleRoutes:
    direct: list[Route] = field(default_factory=list)
    routers: dict[str, tuple[str, list[Route]]] = field(default_factory=dict)
    applications: set[str] = field(default_factory=set)
    imported_app_routes: dict[str, list[Route]] = field(default_factory=dict)
    mounts: list[Mount] = field(default_factory=list)
    imports: dict[str, ImportBinding] = field(default_factory=dict)


def _literal_path(value: ast.expr | None) -> str | None:
    if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.startswith("/"):
        return value.value
    return None


def _prefix(call: ast.Call) -> str | None:
    values = [keyword.value for keyword in call.keywords if keyword.arg == "prefix"]
    if not values:
        return ""
    return _literal_path(values[0]) if len(values) == 1 else None


def _join(*parts: str) -> str:
    return "/" + "/".join(part.strip("/") for part in parts if part.strip("/"))


def _attribute_path(value: ast.expr) -> list[str] | None:
    parts = []
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if not isinstance(value, ast.Name):
        return None
    return [value.id, *reversed(parts)]


def _route_decorators(
    function: ast.FunctionDef | ast.AsyncFunctionDef, file: Path, root: Path, owner: str = "",
) -> list[Route]:
    routes = []
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        receiver = decorator.func.value
        method = METHODS.get(decorator.func.attr)
        path = _literal_path(decorator.args[0] if decorator.args else None)
        if not isinstance(receiver, ast.Name) or method is None or path is None:
            continue
        symbol = f"{module_name(file, root)}.{owner}{function.name}"
        routes.append(Route(file, decorator.lineno, method, path, symbol, receiver.id))
    return routes


def parse_module(file: Path, root: Path) -> ModuleRoutes:
    """Record top-level bindings only; a reassigned or dynamic router is ignored."""
    result = ModuleRoutes()
    try:
        tree = ast.parse(file.read_text(encoding="utf-8", errors="ignore"), filename=str(file))
    except (SyntaxError, OSError):
        return result
    factories: dict[str, str] = {}
    modules: set[str] = set()
    apps: set[str] = set()

    def forget(name: str) -> None:
        factories.pop(name, None)
        modules.discard(name)
        apps.discard(name)
        result.applications.discard(name)
        result.routers.pop(name, None)
        for key in tuple(result.imports):
            if key == name or key.startswith(f"{name}."):
                result.imports.pop(key)

    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                local = alias.asname or alias.name
                forget(local)
                if statement.module == "fastapi" and statement.level == 0 and alias.name in {"FastAPI", "APIRouter"}:
                    factories[local] = alias.name
                elif alias.name != "*" and (statement.module or statement.level):
                    result.imports[local] = ImportBinding(statement.module or "", alias.name, statement.level)
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                dotted = alias.asname is None and "." in alias.name
                if not dotted or local in factories or local in apps or local in result.routers or local in result.imports:
                    forget(local)
                if alias.name == "fastapi":
                    modules.add(local)
                else:
                    result.imports[alias.name if dotted else local] = ImportBinding(
                        alias.name, "", 0, module_import=True,
                    )
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            value = statement.value
            constructor = None
            if isinstance(value, ast.Call):
                if isinstance(value.func, ast.Name):
                    constructor = factories.get(value.func.id)
                elif isinstance(value.func, ast.Attribute) and isinstance(value.func.value, ast.Name):
                    if value.func.value.id in modules and value.func.attr in {"FastAPI", "APIRouter"}:
                        constructor = value.func.attr
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                forget(target.id)
                if len(targets) != 1 or not isinstance(value, ast.Call):
                    continue
                if constructor == "FastAPI":
                    apps.add(target.id)
                    result.applications.add(target.id)
                elif constructor == "APIRouter" and (prefix := _prefix(value)) is not None:
                    result.routers[target.id] = (prefix, [])
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            if (isinstance(call.func, ast.Attribute) and call.func.attr == "include_router"
                    and isinstance(call.func.value, ast.Name)
                    and (call.func.value.id in apps or call.func.value.id in result.routers)
                    and call.args):
                prefix = _prefix(call)
                argument = call.args[0]
                name = argument.id if isinstance(argument, ast.Name) else None
                router_attribute = None
                if isinstance(argument, ast.Attribute) and (path := _attribute_path(argument)):
                    name = ".".join(path[:-1])
                    router_attribute = path[-1]
                if router_attribute is not None:
                    valid_router = name in result.imports
                else:
                    valid_router = name in result.routers or name in result.imports
                if prefix is not None and valid_router:
                    owner = call.func.value.id
                    result.mounts.append(Mount(owner, name, prefix, call.lineno,
                                               result.imports.get(name), owner in apps,
                                               router_attribute))
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            functions = statement.body if isinstance(statement, ast.ClassDef) else [statement]
            owner = f"{statement.name}." if isinstance(statement, ast.ClassDef) else ""
            for function in functions:
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for route in _route_decorators(function, file, root, owner):
                    if route.router in apps:
                        result.direct.append(Route(file, route.line, route.method, route.path, route.symbol))
                    elif route.router in result.routers:
                        result.routers[route.router][1].append(route)
                    elif route.router in result.imports:
                        result.imported_app_routes.setdefault(route.router, []).append(route)
            forget(statement.name)
        elif isinstance(statement, (ast.AugAssign, ast.Delete)):
            targets = [statement.target] if isinstance(statement, ast.AugAssign) else statement.targets
            for target in targets:
                if isinstance(target, ast.Name):
                    forget(target.id)
    return result


def _import_source(file: Path, binding: ImportBinding, root: Path) -> Path | None:
    return imported_module_path(file, root, binding.module, binding.level)


def proven_routes(files: list[Path], root: Path) -> list[Route]:
    """Resolve direct routes and routers imported from local modules, once."""
    root = root.resolve()
    modules = {path.resolve(): parse_module(path.resolve(), root) for path in files if path.suffix == ".py"}

    def mounted_router(mount: Mount, file: Path) -> tuple[Path, str] | None:
        if mount.imported_from:
            binding = mount.imported_from
            if mount.router_attribute:
                source = (imported_module_path(file, root, binding.module, binding.level)
                          if binding.module_import else
                          imported_submodule_path(file, root, binding.module, binding.level, binding.name))
                return (source, mount.router_attribute) if source is not None else None
            source = _import_source(file, binding, root)
            return (source, binding.name) if source is not None else None
        return file, mount.router

    def router_routes(file: Path, name: str, before: int | None,
                      ancestors: frozenset[tuple[Path, str]]) -> list[Route]:
        identity = (file, name)
        if identity in ancestors:
            return []
        module = modules.get(file)
        router = module.routers.get(name) if module else None
        if router is None:
            return []
        prefix, handlers = router
        routes = [Route(route.file, route.line, route.method, _join(prefix, route.path), route.symbol)
                  for route in handlers if before is None or route.line < before]
        for mount in module.mounts:
            if mount.on_app or mount.owner != name or (before is not None and mount.line >= before):
                continue
            target = mounted_router(mount, file)
            if target is None:
                continue
            child_file, child_name = target
            child_before = mount.line if child_file == file else None
            for route in router_routes(child_file, child_name, child_before, ancestors | {identity}):
                routes.append(Route(route.file, route.line, route.method,
                                    _join(prefix, mount.prefix, route.path), route.symbol))
        return routes

    routes: list[Route] = []
    for file, module in modules.items():
        routes.extend(module.direct)
        for name, handlers in module.imported_app_routes.items():
            binding = module.imports.get(name)
            source = _import_source(file, binding, root) if binding else None
            if source is None or binding.name not in modules.get(source, ModuleRoutes()).applications:
                continue
            routes.extend(Route(route.file, route.line, route.method, route.path, route.symbol)
                          for route in handlers)
        for mount in module.mounts:
            if not mount.on_app:
                continue
            target = mounted_router(mount, file)
            if target is None:
                continue
            source, router_name = target
            before = mount.line if source == file else None
            for route in router_routes(source, router_name, before, frozenset()):
                routes.append(Route(route.file, route.line, route.method,
                                    _join(mount.prefix, route.path), route.symbol))
    return list(dict.fromkeys(routes))
