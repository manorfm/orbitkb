"""Unit tests for jvm_security_analyzer's method-level (@PreAuthorize/@Secured)
extraction -- the filter-chain (authorizeHttpRequests) side is exercised at the
StaticAnalysisEngine level in test_static_analysis.py, since it's a whole-service,
cross-file concern.
"""
from __future__ import annotations

from pathlib import Path

from orbitkb.analysis.jvm_security_analyzer import (
    method_security_requirement,
    spring_filter_chain_security_requirements,
)
from orbitkb.analysis.models import Evidence

EVIDENCE = Evidence("OrdersController.kt", 12, 14)


def test_pre_authorize_has_role_extracts_the_role():
    requirement = method_security_requirement("OrdersController.cancel", '@PreAuthorize("hasRole(\'ADMIN\')")', EVIDENCE)

    assert requirement.symbol == "OrdersController.cancel"
    assert requirement.route_pattern is None
    assert requirement.requirement == "hasRole"
    assert requirement.roles == ("ADMIN",)
    assert requirement.evidence == EVIDENCE


def test_pre_authorize_has_any_role_extracts_every_role():
    requirement = method_security_requirement("x", "@PreAuthorize(\"hasAnyRole('ADMIN', 'OWNER')\")", EVIDENCE)

    assert requirement.requirement == "hasAnyRole"
    assert requirement.roles == ("ADMIN", "OWNER")


def test_pre_authorize_has_authority_extracts_the_authority():
    requirement = method_security_requirement("x", "@PreAuthorize(\"hasAuthority('SCOPE_read')\")", EVIDENCE)

    assert requirement.requirement == "hasAuthority"
    assert requirement.roles == ("SCOPE_read",)


def test_pre_authorize_is_authenticated_has_no_roles():
    requirement = method_security_requirement("x", '@PreAuthorize("isAuthenticated()")', EVIDENCE)

    assert requirement.requirement == "authenticated"
    assert requirement.roles == ()


def test_pre_authorize_with_a_composed_spel_expression_is_marked_custom():
    """`hasRole('ADMIN') and #id == authentication.principal.id` mixes a role check
    with an arbitrary condition -- decomposing that into "the role is ADMIN" would
    silently drop the second half, so it's recorded as an unresolved custom rule
    instead, evidence intact.
    """
    requirement = method_security_requirement(
        "x", "@PreAuthorize(\"hasRole('ADMIN') and #id == authentication.principal.id\")", EVIDENCE,
    )

    assert requirement.requirement.startswith("custom:")
    assert requirement.roles == ()


def test_secured_with_a_single_role():
    requirement = method_security_requirement("x", '@Secured("ROLE_ADMIN")', EVIDENCE)

    assert requirement.requirement == "secured"
    assert requirement.roles == ("ROLE_ADMIN",)


def test_secured_with_multiple_roles():
    requirement = method_security_requirement("x", '@Secured({"ROLE_ADMIN", "ROLE_OWNER"})', EVIDENCE)

    assert requirement.requirement == "secured"
    assert requirement.roles == ("ROLE_ADMIN", "ROLE_OWNER")


def test_no_security_annotation_returns_none():
    assert method_security_requirement("x", "@GetMapping(\"/orders\")", EVIDENCE) is None


def _write_filter_chain(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "SecurityConfig.kt"
    path.write_text(
        f'''@Configuration
class SecurityConfig {{
    @Bean
    fun filterChain(http: HttpSecurity): SecurityFilterChain {{
        http {{
            authorizeHttpRequests {{
{body}
            }}
        }}
        return http.build()
    }}
}}
''',
        encoding="utf-8",
    )
    return path


def test_filter_chain_extracts_a_bare_authenticated_rule_for_any_request(tmp_path: Path):
    path = _write_filter_chain(tmp_path, "                authorize(anyRequest, authenticated)")

    requirements = spring_filter_chain_security_requirements([path], tmp_path)

    assert [(r.route_pattern, r.method, r.requirement, r.roles) for r in requirements] == [
        ("**", None, "authenticated", ()),
    ]


def test_filter_chain_extracts_a_pattern_with_an_http_method_and_role_call(tmp_path: Path):
    path = _write_filter_chain(
        tmp_path,
        '                authorize(HttpMethod.POST, "/restaurants/{id}/destinations", hasRole("MANAGER"))',
    )

    requirements = spring_filter_chain_security_requirements([path], tmp_path)

    assert [(r.route_pattern, r.method, r.requirement, r.roles) for r in requirements] == [
        ("/restaurants/{id}/destinations", "POST", "hasRole", ("MANAGER",)),
    ]


def test_filter_chain_marks_a_custom_authorization_manager_without_a_resolvable_policy(tmp_path: Path):
    path = _write_filter_chain(
        tmp_path,
        '                authorize("/clusters/{clusterId}/**", ClusterAccessAuthorizationManager(unknownPolicy()))',
    )

    requirements = spring_filter_chain_security_requirements([path], tmp_path)

    assert [(r.route_pattern, r.requirement, r.roles) for r in requirements] == [
        ("/clusters/{clusterId}/**", "custom:ClusterAccessAuthorizationManager", ()),
    ]


def test_filter_chain_ignores_a_dynamic_route_pattern(tmp_path: Path):
    path = _write_filter_chain(tmp_path, "                authorize(someComputedPattern, authenticated)")

    assert spring_filter_chain_security_requirements([path], tmp_path) == []


def test_filter_chain_ignores_files_that_never_mention_security_filter_chain(tmp_path: Path):
    path = tmp_path / "OrdersController.kt"
    path.write_text("class OrdersController { fun authorize(x: String) {} }", encoding="utf-8")

    assert spring_filter_chain_security_requirements([path], tmp_path) == []
