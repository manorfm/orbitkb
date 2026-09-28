"""Spring Security authorization-requirement extraction for jvm-spring: which role
or authenticated-only rule a route or a specific method requires, proven from
source text -- never from actually evaluating Spring's runtime authorization logic.

Two independent sources feed the same `SecurityRequirement` fact:
- Method-level `@PreAuthorize`/`@Secured` annotations (this module, called from
  jvm_spring_analyzer.py's per-function loop, since it's naturally per-symbol).
- A `SecurityFilterChain` bean's `authorizeHttpRequests { authorize(...) }` DSL
  (this module's `spring_filter_chain_security_requirements`, called from
  engine.py's `enrich()` since it's a whole-service, cross-file concern -- the
  requirement referenced can be defined in a different file than the route it
  guards).

Deliberately conservative: a requirement this module can't reduce to a known
Spring Security expression (`hasRole`, `hasAnyRole`, `hasAuthority`,
`hasAnyAuthority`, `authenticated`, `permitAll`, `denyAll`, or -- one hop --
a custom `AuthorizationManager` built from a local zero-arg policy function)
is recorded as `requirement="custom:<expression>"` with no roles, rather than
silently dropped or guessed at further.
"""
from __future__ import annotations

import re
from pathlib import Path

from orbitkb.analysis.jvm_scanner import find_classes, find_functions, find_matching_paren, split_top_level
from orbitkb.analysis.models import Evidence, SecurityRequirement

_PRE_AUTHORIZE = re.compile(r'@PreAuthorize\s*\(\s*"(?P<expr>[^"]*)"\s*\)')
_SECURED = re.compile(r'@Secured\s*\(\s*(?P<value>\{[^}]*\}|"[^"]*")\s*\)')
_SPEL_ROLE_CALL = re.compile(
    r"^(?P<fn>hasRole|hasAnyRole|hasAuthority|hasAnyAuthority)\s*\(\s*"
    r"(?P<args>'[^']*'(?:\s*,\s*'[^']*')*)\s*\)$",
)
_SPEL_NO_ARG_CALL = re.compile(r"^(?P<fn>isAuthenticated|permitAll|denyAll)\s*\(\s*\)$")
_SINGLE_QUOTED = re.compile(r"'([^']*)'")
_DOUBLE_QUOTED = re.compile(r'"([^"]*)"')

_SPEL_REQUIREMENT_NAME = {
    "hasRole": "hasRole", "hasAnyRole": "hasAnyRole",
    "hasAuthority": "hasAuthority", "hasAnyAuthority": "hasAnyAuthority",
    "isAuthenticated": "authenticated", "permitAll": "permitAll", "denyAll": "denyAll",
}


def _spel_requirement(expr: str) -> tuple[str, tuple[str, ...]]:
    """(requirement, roles) for one SpEL expression -- a single recognized
    Spring Security function call filling the whole expression, or
    `("custom:<expr>", ())` when it's anything else (composed conditions,
    unrecognized functions): SpEL is a full expression language, and only this
    one shape identifies a requirement deterministically from text alone.
    """
    expr = expr.strip()
    call_match = _SPEL_ROLE_CALL.match(expr)
    if call_match:
        roles = tuple(_SINGLE_QUOTED.findall(call_match.group("args")))
        return _SPEL_REQUIREMENT_NAME[call_match.group("fn")], roles
    no_arg_match = _SPEL_NO_ARG_CALL.match(expr)
    if no_arg_match:
        return _SPEL_REQUIREMENT_NAME[no_arg_match.group("fn")], ()
    return f"custom:{expr}", ()


def method_security_requirement(symbol: str, modifiers: str, evidence: Evidence) -> SecurityRequirement | None:
    """A `@PreAuthorize`/`@Secured` rule on one method -- `modifiers` is the
    annotation/modifier text `jvm_scanner`'s `find_functions`/`find_classes`
    already collects immediately before a declaration.
    """
    pre_authorize_match = _PRE_AUTHORIZE.search(modifiers)
    if pre_authorize_match:
        requirement, roles = _spel_requirement(pre_authorize_match.group("expr"))
        return SecurityRequirement(None, None, symbol, requirement, roles, evidence)
    secured_match = _SECURED.search(modifiers)
    if secured_match:
        roles = tuple(_DOUBLE_QUOTED.findall(secured_match.group("value")))
        return SecurityRequirement(None, None, symbol, "secured", roles, evidence)
    return None


# The route-level `authorizeHttpRequests { authorize(...) }` DSL is real Kotlin
# source, not a SpEL string -- its own string literals are plain double-quoted
# Kotlin strings, so it needs its own (double-quote) variant of the same shapes
# `_spel_requirement` recognizes for the (single-quoted, string-embedded) SpEL
# form `@PreAuthorize` uses. Two grammars for the same underlying idea, not one
# path duplicated: reusing `_spel_requirement`'s regex here would silently fail
# on every real DSL call, since none of its arguments are single-quoted.
_AUTHORIZE_CALL = re.compile(r"(?<![.\w])authorize\s*\(")
_DSL_ROLE_CALL = re.compile(
    r'^(?P<fn>hasRole|hasAnyRole|hasAuthority|hasAnyAuthority)\s*\(\s*'
    r'(?P<args>"[^"]*"(?:\s*,\s*"[^"]*")*)\s*\)$',
)
_DSL_BARE_KEYWORDS = frozenset({"authenticated", "permitAll", "denyAll"})
_HTTP_METHOD_ARG = re.compile(r"^HttpMethod\.(?P<method>[A-Z]+)$")
_STRING_LITERAL_ARG = re.compile(r'^"(?P<value>[^"]*)"$')
# A custom `AuthorizationManager`, e.g. `RestaurantAccessAuthorizationManager(administrationPolicy())`
# -- one hop of role resolution into a local zero-arg policy function is handled
# by the caller (`spring_filter_chain_security_requirements`), which has the
# whole-service file list this module-level parser deliberately doesn't need.
_CONSTRUCTOR_CALL = re.compile(r"^(?P<class_name>[A-Za-z_]\w*)\s*\((?P<args>.*)\)$", re.DOTALL)


def _dsl_requirement(expr: str) -> tuple[str, tuple[str, ...]]:
    """(requirement, roles) for one `authorize(...)` requirement argument --
    a bare DSL keyword, a recognized role/authority call, a custom
    `AuthorizationManager` constructor call (roles resolved by the caller), or
    `("custom:<expr>", ())` for anything else.
    """
    expr = expr.strip()
    if expr in _DSL_BARE_KEYWORDS:
        return expr, ()
    call_match = _DSL_ROLE_CALL.match(expr)
    if call_match:
        return call_match.group("fn"), tuple(_DOUBLE_QUOTED.findall(call_match.group("args")))
    constructor_match = _CONSTRUCTOR_CALL.match(expr)
    if constructor_match:
        return f"custom:{constructor_match.group('class_name')}", ()
    return f"custom:{expr}", ()


def _authorize_pattern(arg: str) -> str | None:
    """The literal route pattern one `authorize(...)` argument names, or `None`
    when it isn't a provable literal (a variable reference, a computed
    expression) -- `anyRequest` becomes the Ant-style `"**"` wildcard already
    used elsewhere in this DSL, so a route-level fact's `route_pattern` is never
    `None` (that's reserved for a method-level fact, which has no route pattern
    at all).
    """
    if arg == "anyRequest":
        return "**"
    literal_match = _STRING_LITERAL_ARG.match(arg)
    return literal_match.group("value") if literal_match else None


def _parse_authorize_args(args: list[str]) -> tuple[str | None, str, str] | None:
    """(method, route_pattern, requirement_expr) from one `authorize(...)`
    call's already-split arguments, or `None` when the call isn't one of the
    two shapes this DSL uses (`authorize(pattern, requirement)` or
    `authorize(HttpMethod.X, pattern, requirement)`) or its pattern isn't a
    provable literal.
    """
    if len(args) == 3:
        method_match = _HTTP_METHOD_ARG.match(args[0])
        if method_match is None:
            return None
        pattern = _authorize_pattern(args[1])
        return None if pattern is None else (method_match.group("method"), pattern, args[2])
    if len(args) == 2:
        pattern = _authorize_pattern(args[0])
        return None if pattern is None else (None, pattern, args[1])
    return None


def spring_filter_chain_security_requirements(files: list[Path], root: Path) -> list[SecurityRequirement]:
    """Every `authorize(...)` rule in a `SecurityFilterChain` bean's
    `authorizeHttpRequests` DSL block, across the service's own files -- the bean
    can live in any file, not necessarily one already known to hold an endpoint.
    """
    requirements: list[SecurityRequirement] = []
    for path in files:
        if path.suffix not in {".java", ".kt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "SecurityFilterChain" not in text:
            continue
        for class_match in find_classes(text):
            for function_match in find_functions(text, class_match.body_start, class_match.body_end, kotlin=path.suffix == ".kt"):
                signature = function_match.text[: function_match.body_offset]
                if "SecurityFilterChain" not in signature:
                    continue
                body = function_match.text[function_match.body_offset :]
                for call_match in _AUTHORIZE_CALL.finditer(body):
                    open_paren = call_match.end() - 1
                    close_paren = find_matching_paren(body, open_paren)
                    if close_paren == -1:
                        continue
                    args = [a.strip() for a in split_top_level(body[open_paren + 1 : close_paren], ",")]
                    parsed = _parse_authorize_args(args)
                    if parsed is None:
                        continue
                    method, pattern, requirement_expr = parsed
                    requirement, roles = _dsl_requirement(requirement_expr)
                    line = function_match.start_line + function_match.text.count(
                        "\n", 0, function_match.body_offset + call_match.start(),
                    )
                    requirements.append(SecurityRequirement(
                        pattern, method, None, requirement, roles,
                        Evidence(path.relative_to(root).as_posix(), line, line),
                    ))
    return requirements
