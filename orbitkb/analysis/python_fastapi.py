"""Source-proven FastAPI routes, including literal APIRouter mounts."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

METHODS = {name: name.upper() for name in ("get", "post", "put", "patch", "delete", "head", "options")}


@dataclass(frozen=True)
class Route:
    file: Path
    line: int
    method: str
    path: str
    symbol: str
    router: str | None = None


@dataclass
class ModuleRoutes:
    direct: list[Route] = field(default_factory=list)
    routers: dict[str, tuple[str, list[Route]]] = field(default_factory=dict)
    applications: set[str] = field(default_factory=set)
    imported_app_routes: dict[str, list[Route]] = field(default_factory=dict)
    mounts: list[tuple[str, str]] = field(default_factory=list)
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)


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


def _route_decorators(function: ast.FunctionDef | ast.AsyncFunctionDef, file: Path, owner: str = "") -> list[Route]:
    routes = []
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        receiver = decorator.func.value
        method = METHODS.get(decorator.func.attr)
        path = _literal_path(decorator.args[0] if decorator.args else None)
        if not isinstance(receiver, ast.Name) or method is None or path is None:
            continue
        symbol = f"{file.stem}.{owner}{function.name}"
        routes.append(Route(file, decorator.lineno, method, path, symbol, receiver.id))
    return routes


def parse_module(file: Path) -> ModuleRoutes:
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
        result.imports.pop(name, None)

    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                local = alias.asname or alias.name
                forget(local)
                if statement.module == "fastapi" and statement.level == 0 and alias.name in {"FastAPI", "APIRouter"}:
                    factories[local] = alias.name
                elif statement.module and alias.name != "*":
                    result.imports[local] = (statement.module, alias.name)
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                forget(local)
                if alias.name == "fastapi":
                    modules.add(local)
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
                    and isinstance(call.func.value, ast.Name) and call.func.value.id in apps
                    and call.args and isinstance(call.args[0], ast.Name)):
                prefix = _prefix(call)
                if prefix is not None:
                    result.mounts.append((call.args[0].id, prefix))
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            functions = statement.body if isinstance(statement, ast.ClassDef) else [statement]
            owner = f"{statement.name}." if isinstance(statement, ast.ClassDef) else ""
            for function in functions:
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for route in _route_decorators(function, file, owner):
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


def proven_routes(files: list[Path], root: Path) -> list[Route]:
    """Resolve direct routes and routers imported from local modules, once."""
    modules = {path.resolve(): parse_module(path) for path in files if path.suffix == ".py"}
    root = root.resolve()
    routes: list[Route] = []
    for file, module in modules.items():
        routes.extend(module.direct)
        for name, handlers in module.imported_app_routes.items():
            dotted_module, app_name = module.imports[name]
            source = (root / Path(*dotted_module.split("."))).with_suffix(".py").resolve()
            if not source.is_relative_to(root) or app_name not in modules.get(source, ModuleRoutes()).applications:
                continue
            routes.extend(Route(route.file, route.line, route.method, route.path, route.symbol)
                          for route in handlers)
        for name, mount_prefix in module.mounts:
            source = file
            router_name = name
            if name in module.imports:
                dotted_module, router_name = module.imports[name]
                source = (root / Path(*dotted_module.split("."))).with_suffix(".py").resolve()
                if not source.is_relative_to(root):
                    continue
            router = modules.get(source, ModuleRoutes()).routers.get(router_name)
            if router is None:
                continue
            router_prefix, handlers = router
            for handler in handlers:
                routes.append(Route(handler.file, handler.line, handler.method,
                                    _join(mount_prefix, router_prefix, handler.path), handler.symbol))
    return list(dict.fromkeys(routes))
