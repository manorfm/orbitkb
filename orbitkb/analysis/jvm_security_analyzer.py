"""Spring Security authorization-requirement extraction for jvm-spring: which role
or authenticated-only rule a route or a specific method requires, proven from
source text -- never from actually evaluating Spring's runtime authorization logic.

Two independent sources feed the same `SecurityRequirement` fact:
- Method-level `@PreAuthorize`/`@Secured` annotations (this module, called from
  jvm_spring_analyzer.py's per-function loop, since it's naturally per-symbol).
- A `SecurityFilterChain` bean's Kotlin `authorizeHttpRequests { authorize(...) }`
  DSL or Java `authorizeHttpRequests(...requestMatchers(...).permitAll())` chain
  (this module's `spring_filter_chain_security_requirements`, called through
  `SpringSecurityAdapter` since it's a whole-service, cross-file concern -- the
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

from orbitkb.analysis.jvm_scanner import (
    find_classes,
    find_functions,
    find_matching_paren,
    split_top_level,
)
from orbitkb.analysis.models import AnalysisResult, Evidence, SecurityRequirement

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
_JAVA_AUTHORIZE_CALL = re.compile(r"\bauthorizeHttpRequests\s*\(")
_JAVA_MATCHER_CALL = re.compile(r"\.\s*(requestMatchers|anyRequest)\s*\(")
_JAVA_TERMINAL_CALL = re.compile(r"\s*\.\s*([A-Za-z_]\w*)\s*\(")
_DYNAMIC_ROUTE_REQUIREMENT = "custom:dynamic_route_pattern"
_DSL_ROLE_CALL = re.compile(
    r'^(?P<fn>hasRole|hasAnyRole|hasAuthority|hasAnyAuthority)\s*\(\s*'
    r'(?P<args>"[^"]*"(?:\s*,\s*"[^"]*")*)\s*\)$',
)
_DSL_BARE_KEYWORDS = frozenset({"authenticated", "permitAll", "denyAll"})
_HTTP_METHOD_ARG = re.compile(r"^HttpMethod\.(?P<method>[A-Z]+)$")
_STRING_LITERAL_ARG = re.compile(r'^"(?P<value>[^"]*)"$')
# A custom `AuthorizationManager`, e.g. `RestaurantAccessAuthorizationManager(administrationPolicy())`.
# One hop of role resolution into a local, zero-arg Kotlin policy function
# (`fun administrationPolicy() = AuthorizationPolicy(accountRoles = setOf("OWNER", ...), ...)`,
# a real, common pattern for keeping a policy's roles in one place instead of
# repeating them at every route) -- every double-quoted literal in that
# function's own body becomes a candidate role. Not a proof of how the manager
# actually combines them (see module docstring); just what its own code names.
_CONSTRUCTOR_CALL = re.compile(r"^(?P<class_name>[A-Za-z_]\w*)\s*\((?P<args>.*)\)$", re.DOTALL)
_ZERO_ARG_CALL = re.compile(r"^(?P<fn>[A-Za-z_]\w*)\s*\(\s*\)$")
_ZERO_ARG_FUNCTION_SIGNATURE = re.compile(r"^fun\s+\w+\(\s*\)")


def _index_zero_arg_policy_functions(files: list[Path]) -> dict[str, tuple[str, ...]]:
    """name -> every double-quoted string literal in one local, zero-arg Kotlin
    function's body/expression -- the candidate roles for whichever
    `AuthorizationManager` constructor calls it. The first definition of a given
    name wins; a real duplicate top-level name is already ambiguous Kotlin.
    """
    roles_by_name: dict[str, tuple[str, ...]] = {}
    for path in files:
        if path.suffix != ".kt":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for function_match in find_functions(text, 0, len(text), kotlin=True):
            if function_match.name in roles_by_name or not _ZERO_ARG_FUNCTION_SIGNATURE.match(function_match.text):
                continue
            body = function_match.text[function_match.body_offset :]
            roles_by_name[function_match.name] = tuple(_DOUBLE_QUOTED.findall(body))
    return roles_by_name


def _dsl_requirement(expr: str, policy_roles: dict[str, tuple[str, ...]]) -> tuple[str, tuple[str, ...]]:
    """(requirement, roles) for one `authorize(...)` requirement argument --
    a bare DSL keyword, a recognized role/authority call, a custom
    `AuthorizationManager` constructor call (roles resolved one hop via
    `policy_roles`, see `_index_zero_arg_policy_functions`), or
    `("custom:<expr>", ())` for anything else.
    """
    expr = expr.strip()
    if expr == _DYNAMIC_ROUTE_REQUIREMENT:
        return expr, ()
    if expr in _DSL_BARE_KEYWORDS:
        return expr, ()
    call_match = _DSL_ROLE_CALL.match(expr)
    if call_match:
        return call_match.group("fn"), tuple(_DOUBLE_QUOTED.findall(call_match.group("args")))
    constructor_match = _CONSTRUCTOR_CALL.match(expr)
    if constructor_match:
        roles: list[str] = []
        for arg in split_top_level(constructor_match.group("args"), ","):
            zero_arg_match = _ZERO_ARG_CALL.match(arg.strip())
            if zero_arg_match:
                roles.extend(policy_roles.get(zero_arg_match.group("fn"), ()))
        return f"custom:{constructor_match.group('class_name')}", tuple(dict.fromkeys(roles))
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
    """Return a literal rule or an unknown wildcard for a computed pattern.
    """
    if len(args) == 3:
        method_match = _HTTP_METHOD_ARG.match(args[0])
        if method_match is None:
            return None
        pattern = _authorize_pattern(args[1])
        return (method_match.group("method"), pattern or "**",
                args[2] if pattern is not None else _DYNAMIC_ROUTE_REQUIREMENT)
    if len(args) == 2:
        pattern = _authorize_pattern(args[0])
        return (None, pattern or "**", args[1] if pattern is not None else _DYNAMIC_ROUTE_REQUIREMENT)
    return None


def _java_filter_chain_requirements(
    body: str, path: Path, root: Path, start_line: int,
) -> list[SecurityRequirement]:
    """Read literal Java request matchers and their immediately chained rule."""
    requirements: list[SecurityRequirement] = []
    for authorize in _JAVA_AUTHORIZE_CALL.finditer(body):
        authorize_end = find_matching_paren(body, authorize.end() - 1)
        if authorize_end == -1:
            continue
        segment = body[authorize.end():authorize_end]
        for matcher in _JAVA_MATCHER_CALL.finditer(segment):
            matcher_end = find_matching_paren(segment, matcher.end() - 1)
            if matcher_end == -1:
                continue
            args = [item.strip() for item in split_top_level(segment[matcher.end():matcher_end], ",")]
            if matcher.group(1) == "anyRequest" and not any(args):
                method, pattern = None, "**"
            elif matcher.group(1) == "requestMatchers" and len(args) == 2:
                method_match = _HTTP_METHOD_ARG.fullmatch(args[0])
                method = method_match.group("method") if method_match else None
                pattern = _authorize_pattern(args[1]) if method_match else None
            elif matcher.group(1) == "requestMatchers" and len(args) == 1:
                method, pattern = None, _authorize_pattern(args[0])
            else:
                continue
            if pattern is None:
                offset = authorize.end() + matcher.start()
                line = start_line + body.count("\n", 0, offset)
                requirements.append(SecurityRequirement(
                    "**", method, None, _DYNAMIC_ROUTE_REQUIREMENT, (),
                    Evidence(path.relative_to(root).as_posix(), line, line),
                ))
                continue
            terminal = _JAVA_TERMINAL_CALL.match(segment, matcher_end + 1)
            if terminal is None:
                continue
            terminal_end = find_matching_paren(segment, terminal.end() - 1)
            if terminal_end == -1:
                continue
            name = terminal.group(1)
            terminal_args = segment[terminal.end():terminal_end].strip()
            if name in _DSL_BARE_KEYWORDS and not terminal_args:
                requirement, roles = name, ()
            elif name in {"hasRole", "hasAuthority"} and _STRING_LITERAL_ARG.fullmatch(terminal_args):
                requirement, roles = name, (terminal_args[1:-1],)
            else:
                requirement, roles = f"custom:{name}", ()
            offset = authorize.end() + matcher.start()
            line = start_line + body.count("\n", 0, offset)
            requirements.append(SecurityRequirement(
                pattern, method, None, requirement, roles,
                Evidence(path.relative_to(root).as_posix(), line, line),
            ))
    return requirements


def spring_filter_chain_security_requirements(files: list[Path], root: Path) -> list[SecurityRequirement]:
    """Literal Kotlin or Java route rules in a `SecurityFilterChain` method,
    across the service's own files -- the bean
    can live in any file, not necessarily one already known to hold an endpoint.
    """
    policy_roles = _index_zero_arg_policy_functions(files)
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
                if path.suffix == ".java":
                    requirements.extend(_java_filter_chain_requirements(
                        body, path, root,
                        function_match.start_line + signature.count("\n"),
                    ))
                    continue
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
                    requirement, roles = _dsl_requirement(requirement_expr, policy_roles)
                    line = function_match.start_line + function_match.text.count(
                        "\n", 0, function_match.body_offset + call_match.start(),
                    )
                    requirements.append(SecurityRequirement(
                        pattern, method, None, requirement, roles,
                        Evidence(path.relative_to(root).as_posix(), line, line),
                    ))
    return requirements


class SpringSecurityAdapter:
    """Add source-proven Spring filter-chain rules after per-file analysis."""

    def enrich(self, result: AnalysisResult, files: list[Path], root: Path) -> None:
        result.security_requirements.extend(spring_filter_chain_security_requirements(files, root))
