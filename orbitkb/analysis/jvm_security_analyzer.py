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
