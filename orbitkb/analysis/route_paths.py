"""Shared route path composition for static source analyzers."""


def join_route(prefix: str | None, route: str | None) -> str | None:
    if route is None or prefix is None:
        return route
    if not route:
        return prefix.rstrip("/") or "/"
    return f"{prefix.rstrip('/')}/{route.lstrip('/')}"
