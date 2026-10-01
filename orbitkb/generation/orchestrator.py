from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from orbitkb.analysis.depth import DepthProvider, NoopDepthProvider
from orbitkb.analysis.engine import STATIC_ANALYSIS_INPUT_VERSION, StaticAnalysisEngine
from orbitkb.ci.scanner import scan_github_actions_commands
from orbitkb.db.repositories import canonical_snapshots as canonical_snapshots_repo
from orbitkb.db.repositories import ci_commands as ci_commands_repo
from orbitkb.db.repositories import cloud_iac as cloud_iac_repo
from orbitkb.db.repositories import embeddings as embeddings_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import index_runs as index_runs_repo
from orbitkb.db.repositories import indexed_files as indexed_files_repo
from orbitkb.db.repositories import (
    kubernetes_configuration as kubernetes_configuration_repo,
)
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.db.repositories import search as search_repo
from orbitkb.db.repositories import security_findings as security_findings_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.db.repositories import source_units as source_units_repo
from orbitkb.db.repositories import static_analysis as static_analysis_repo
from orbitkb.discovery.base import (
    CodeExcerpt,
    EndpointHint,
    ServiceHints,
    StackDetector,
)
from orbitkb.discovery.hashing import file_hash, git_head_commit
from orbitkb.discovery.registry import detector_by_id
from orbitkb.discovery.scan_helpers import SKIP_DIRS, collect_config_excerpts
from orbitkb.discovery.walker import ServiceCandidate, discover_services
from orbitkb.domain.canonical import CanonicalSnapshot
from orbitkb.domain.sufficiency import (
    DeterministicSufficiencyEvaluator,
    SufficiencyResult,
)
from orbitkb.generation.architecture import recompute_architecture_view
from orbitkb.generation.backend_base import LLMBackend, LLMUsage
from orbitkb.generation.budget import ModelBudget
from orbitkb.generation.deterministic_endpoint import render_simple_endpoint
from orbitkb.generation.embeddings import EmbeddingBackend
from orbitkb.generation.evidence import AggregateEvidenceSource, EvidenceSource
from orbitkb.generation.input_digest import component_input_digest
from orbitkb.generation.knowledge import (
    ComponentDocumentation,
    ComponentSummary,
    EndpointDocumentation,
    KnowledgeReader,
    KnowledgeWriter,
    MessageDocumentation,
    MessagingDocumentation,
    OverviewDocumentation,
    PersistenceDocumentation,
    PersistenceEntity,
    compose_component_summaries,
    compose_endpoint_context,
)
from orbitkb.generation.legacy_knowledge import LegacyKnowledgeAdapter
from orbitkb.generation.llm_harness import generate_with_retry, load_prompt, load_schema
from orbitkb.generation.policy import GenerationPolicy
from orbitkb.generation.route_evidence import (
    route_capsule,
    route_documentation_state,
    route_outbound_hints,
)
from orbitkb.generation.source_units import SourceUnit, SourceUnitCollector
from orbitkb.generation.unit import IndexUnit
from orbitkb.iac.scanner import scan_repository_facts
from orbitkb.security.findings import find_security_findings
from orbitkb.security.redaction import redact_sensitive_values

logger = logging.getLogger(__name__)

MAX_EXCERPT_CHARS = 20_000


class DiscoveryError(Exception):
    """Raised when index_path cannot find/resolve a service to index."""


@dataclass
class IndexResult:
    service_name: str
    service_id: int
    files_changed: int
    llm_calls: int
    status: str
    llm_invocations: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    sufficiency_shadow: dict[str, int] = field(default_factory=dict)
    sufficiency_details: tuple[RouteSufficiency, ...] = ()


@dataclass(frozen=True)
class RouteSufficiency:
    method: str
    path: str
    assessment: SufficiencyResult | None
    render_status: str = "ineligible"

    @property
    def status(self) -> str:
        return self.assessment.overall.value if self.assessment else "unassessed"


class ProgressReporter(Protocol):
    """Lets the CLI render progress without generation logic depending on a UI library.

    total_units in service_started is the number of generation "slots" for this
    service (overview + one per endpoint + one per component + persistence + messaging,
    when present) — every slot gets exactly one unit_finished call, whether it was
    actually generated or skipped because nothing changed, so a caller can drive an
    accurate percentage.
    """

    def service_started(self, service: str, total_units: int) -> None: ...
    def unit_started(self, service: str, label: str) -> None: ...
    def unit_finished(self, service: str, label: str, status: str) -> None: ...
    def service_finished(self, service: str) -> None: ...


class NullProgressReporter:
    def service_started(self, service: str, total_units: int) -> None:
        pass

    def unit_started(self, service: str, label: str) -> None:
        pass

    def unit_finished(self, service: str, label: str, status: str) -> None:
        pass

    def service_finished(self, service: str) -> None:
        pass


# ---------------------------------------------------------------------------
# prompt rendering helpers
# ---------------------------------------------------------------------------

def _folder_tree(root: Path, max_depth: int = 2, max_lines: int = 200) -> str:
    lines: list[str] = []
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        depth = len(current.parts) - root_depth
        if depth >= max_depth:
            dirnames[:] = []
        indent = "  " * depth
        if current != root:
            lines.append(f"{indent}{current.name}/")
        if depth < max_depth:
            for f in sorted(filenames)[:20]:
                lines.append(f"{indent}  {f}")
        if len(lines) >= max_lines:
            break
    return "\n".join(lines[:max_lines]) or "(empty)"


def _render_service_overview_prompt(
    name: str, stack: str, root: Path, hints: ServiceHints, components: list[ComponentSummary]
) -> str:
    """Composed LAST in a service's generation run, from the components' own already-
    written summaries — never a fresh read of the entrypoint alone — so the overview
    reflects what the endpoints/classes actually turned out to do, not a guess made
    before any of them were analyzed."""
    entry_file = hints.entry_excerpt.file_path if hints.entry_excerpt else "(none found)"
    entry_excerpt = redact_sensitive_values(hints.entry_excerpt.text) if hints.entry_excerpt else "(no entrypoint file detected)"
    component_summaries = compose_component_summaries(components)
    return load_prompt("service_overview").substitute(
        service_name=name,
        stack=stack,
        folder_tree=_folder_tree(root),
        entry_file=entry_file,
        entry_excerpt=entry_excerpt,
        component_summaries=redact_sensitive_values(component_summaries),
    )


def _render_component_prompt(service_name: str, component_name: str, component_file: str, endpoint_summaries: str) -> str:
    return load_prompt("component").substitute(
        service_name=service_name, component_name=component_name, component_file=component_file,
        endpoint_summaries=endpoint_summaries,
    )


def _group_endpoints_by_component(endpoints: list[EndpointHint]) -> dict[str, list[EndpointHint]]:
    groups: dict[str, list[EndpointHint]] = {}
    for endpoint in endpoints:
        groups.setdefault(endpoint.component_hint, []).append(endpoint)
    return groups


def _unique_endpoint_routes(endpoints: list[EndpointHint]) -> list[EndpointHint]:
    """One generation per API key, retaining evidence from repeated route hints."""
    by_route: dict[tuple[str, str], EndpointHint] = {}
    for endpoint in endpoints:
        key = (endpoint.method, endpoint.path)
        if key not in by_route:
            by_route[key] = replace(endpoint, extra_excerpts=list(endpoint.extra_excerpts))
            continue
        current = by_route[key]
        seen = {current.excerpt, *current.extra_excerpts}
        for excerpt in (endpoint.excerpt, *endpoint.extra_excerpts):
            if excerpt not in seen:
                current.extra_excerpts.append(excerpt)
                seen.add(excerpt)
    return list(by_route.values())


def _render_api_detail_prompt(
    name: str, stack: str, endpoint: EndpointHint, hints: ServiceHints,
    evidence_source: EvidenceSource | None = None,
    outbound_evidence: str | None = None,
) -> str:
    source = evidence_source or EvidenceSource.from_excerpts(
        [endpoint.excerpt, *endpoint.extra_excerpts], MAX_EXCERPT_CHARS,
    )
    code = source.prompt_text
    dep_files = endpoint.dependency_files()
    own_calls = [c for c in hints.outbound_calls if c.excerpt.file_path in dep_files]
    legacy_outbound = "\n".join(
        f"- [{c.call_kind}] {c.target_hint} ({c.excerpt.file_path}:{c.excerpt.start_line})"
        for c in own_calls[:30]
    ) or "(none found)"
    outbound = legacy_outbound
    if outbound_evidence:
        outbound = outbound_evidence if legacy_outbound == "(none found)" else f"{legacy_outbound}\n{outbound_evidence}"
    return load_prompt("api_detail").substitute(
        service_name=name,
        stack=stack,
        method=endpoint.method,
        path=endpoint.path,
        code_excerpts=code,
        outbound_call_hints=outbound,
    )


def _persistence_needs_config_evidence(hints: ServiceHints) -> bool:
    """An ORM model/entity definition (SQLAlchemy, JPA, GORM) rarely names its own
    engine — true whenever at least one entity has no manifest-derived engine_hint."""
    return any(p.engine_hint is None for p in hints.persistence)


def _render_persistence_prompt(
    name: str, stack: str, hints: ServiceHints, config_excerpts: list[CodeExcerpt],
    evidence_source: AggregateEvidenceSource | None = None,
) -> str:
    source = evidence_source or AggregateEvidenceSource.from_groups(
        [p.excerpt for p in hints.persistence], config_excerpts, MAX_EXCERPT_CHARS,
    )
    engine_hints = "\n".join(
        f"- {p.name_hint} ({p.excerpt.file_path}:{p.excerpt.start_line}): {p.engine_hint or 'unknown'}"
        for p in hints.persistence
    ) or "(none found)"
    return load_prompt("persistence").substitute(
        service_name=name, stack=stack, persistence_excerpts=source.primary.prompt_text,
        engine_hints=engine_hints, config_evidence=source.config_text,
    )


def _needs_config_evidence(hints: ServiceHints) -> bool:
    """A messaging hint tagged 'abstracted' matched a transport-agnostic API (JMS,
    Celery, NestJS microservices) whose concrete broker only lives in configuration."""
    return any(m.provider_hint == "abstracted" for m in hints.messaging)


def _render_messaging_prompt(
    name: str, stack: str, hints: ServiceHints, config_excerpts: list[CodeExcerpt],
    evidence_source: AggregateEvidenceSource | None = None,
) -> str:
    source = evidence_source or AggregateEvidenceSource.from_groups(
        [m.excerpt for m in hints.messaging], config_excerpts, MAX_EXCERPT_CHARS,
    )
    provider_hints = "\n".join(
        f"- {m.direction} {m.channel_hint} ({m.excerpt.file_path}:{m.excerpt.start_line}): "
        f"{m.provider_hint or 'unknown'}"
        for m in hints.messaging
    ) or "(none found)"
    return load_prompt("messaging").substitute(
        service_name=name, stack=stack, messaging_excerpts=source.primary.prompt_text,
        provider_hints=provider_hints, config_evidence=source.config_text,
    )


# ---------------------------------------------------------------------------
# per-service indexing: one UnitGenerator per generation-unit kind (Strategy),
# coordinated in a fixed order by index_service (Template Method) — see the
# "Hierarchical generation" note on _render_service_overview_prompt for why
# that order (endpoints -> components -> persistence/messaging -> overview)
# is load-bearing, not incidental.
# ---------------------------------------------------------------------------

@dataclass
class UnitOutcome:
    """Generator totals and per-unit results for incremental retry."""

    llm_calls: int = 0
    had_failure: bool = False
    usage: LLMUsage = field(default_factory=LLMUsage)
    backend_duration_ms: float = 0.0
    units: list[IndexUnit] = field(default_factory=list)
    generated_units: int = 0

    def add(self, unit: IndexUnit) -> None:
        self.units.append(unit)
        if unit.status == "success":
            self.generated_units += 1
            if unit.llm_invocations:
                self.llm_calls += 1
        self.had_failure = self.had_failure or unit.status == "failed"
        self.usage = self.usage + unit.usage
        self.backend_duration_ms += unit.backend_duration_ms


@dataclass
class IndexContext:
    """Shared state one index_service call passes through every UnitGenerator.
    Endpoint and component change flags are read by OverviewGenerator (composed last)."""

    conn: sqlite3.Connection
    name: str
    root: Path
    detector: StackDetector
    backend: LLMBackend
    hints: ServiceHints
    component_groups: dict[str, list[EndpointHint]]
    service_id: int
    is_new: bool
    existing: sqlite3.Row | None
    changed: set[str]
    removed: set[str]
    force: bool
    failures_root: Path
    progress: ProgressReporter
    knowledge_reader: KnowledgeReader
    knowledge_writer: KnowledgeWriter
    generation_policy: GenerationPolicy
    previous_snapshot: CanonicalSnapshot | None
    budget: ModelBudget
    source_units_by_route: dict[tuple[str, str], tuple[SourceUnit, ...]] = field(default_factory=dict)
    changed_source_unit_keys: set[str] = field(default_factory=set)
    embedding_backend: EmbeddingBackend | None = None
    regenerated_endpoints: set[tuple[str, str]] = field(default_factory=set)
    endpoint_inventory_changed: bool = False
    any_component_regenerated: bool = False
    sufficiency_shadow: dict[str, int] = field(default_factory=dict)
    sufficiency_details: list[RouteSufficiency] = field(default_factory=list)

    @property
    def llm_invocations(self) -> int:
        return self.budget.invocations

    def record_llm_invocation(self, unit: IndexUnit) -> bool:
        if not self.budget.start_attempt():
            return False
        unit.record_attempt()
        return True

    def record_usage(self, unit: IndexUnit, usage: LLMUsage) -> None:
        unit.record_usage(usage)
        self.budget.record_usage(usage)


class UnitGenerator(Protocol):
    kind: str

    def run(self, ctx: IndexContext) -> UnitOutcome: ...


class EndpointGenerator:
    """Finest-grained unit, generated first so components/overview can compose from
    its summaries (see the module-level docstring above)."""

    kind = "endpoint"

    def run(self, ctx: IndexContext) -> UnitOutcome:
        outcome = UnitOutcome()
        snapshot = canonical_snapshots_repo.read_snapshot(ctx.conn, ctx.service_id)
        existing_keys = ctx.knowledge_reader.endpoint_keys(ctx.service_id)
        keep_api_keys: set[tuple[str, str]] = set()
        for endpoint in ctx.hints.endpoints:
            key = (endpoint.method, endpoint.path)
            unit = IndexUnit(self.kind, key)
            keep_api_keys.add(key)
            dep_files = endpoint.dependency_files()
            source_units = ctx.source_units_by_route.get(key, ())
            needs_regen = (
                ctx.force or key not in existing_keys or bool(dep_files & ctx.changed)
                or any(unit.unit_key in ctx.changed_source_unit_keys for unit in source_units)
            )
            if not needs_regen and snapshot is not None and ctx.previous_snapshot is not None:
                needs_regen = (
                    route_documentation_state(route_capsule(ctx.previous_snapshot, *key))
                    != route_documentation_state(route_capsule(snapshot, *key))
                )
            if not needs_regen:
                needs_regen = index_runs_repo.last_run_unit_failed(
                    ctx.conn, ctx.service_id, self.kind, key,
                )
            label = f"{endpoint.method} {endpoint.path}"
            if not needs_regen:
                outcome.add(unit)
                ctx.progress.unit_finished(ctx.name, label, "skipped")
                continue
            ctx.progress.unit_started(ctx.name, label)
            capsule = route_capsule(snapshot, endpoint.method, endpoint.path) if snapshot else None
            sufficiency = DeterministicSufficiencyEvaluator().evaluate(capsule) if capsule else None
            static_doc = render_simple_endpoint(capsule, sufficiency) if capsule and sufficiency else None
            detail = RouteSufficiency(
                endpoint.method, endpoint.path, sufficiency,
                render_status="used" if static_doc is not None else "ineligible",
            )
            ctx.sufficiency_details.append(detail)
            shadow_status = detail.status
            ctx.sufficiency_shadow[shadow_status] = ctx.sufficiency_shadow.get(shadow_status, 0) + 1
            excerpts = [endpoint.excerpt, *endpoint.extra_excerpts]
            for source_unit in source_units:
                excerpt = source_unit.excerpt
                if not any(
                    current.file_path == excerpt.file_path
                    and current.start_line <= excerpt.start_line and current.end_line >= excerpt.end_line
                    for current in excerpts
                ):
                    excerpts.append(excerpt)
            source = EvidenceSource.from_excerpts(excerpts, MAX_EXCERPT_CHARS)
            if static_doc is None:
                outbound_evidence = (
                    route_outbound_hints(snapshot, endpoint.method, endpoint.path) if snapshot else None
                )
                prompt = _render_api_detail_prompt(
                    ctx.name, ctx.detector.id, endpoint, ctx.hints, source,
                    outbound_evidence=outbound_evidence,
                )
                generation = generate_with_retry(
                    ctx.backend, prompt, load_schema("api_detail"), ctx.root, ctx.failures_root,
                    f"{ctx.name}-{endpoint.method}-{endpoint.path}",
                    on_attempt=lambda unit=unit: ctx.record_llm_invocation(unit),
                    on_usage=lambda usage, unit=unit: ctx.record_usage(unit, usage),
                    on_duration_ms=unit.record_duration,
                    on_prompt=unit.record_prompt,
                )
                if not generation:
                    unit.status = "failed"
                    outcome.add(unit)
                    ctx.progress.unit_finished(ctx.name, label, "failed")
                    continue
                result = generation.structured
            else:
                result = static_doc
            evidence = source.pointers
            ctx.knowledge_writer.save_endpoint(
                ctx.service_id,
                EndpointDocumentation(
                    method=endpoint.method, path=endpoint.path,
                    summary=result["summary"], description=result["description"],
                    response_shape=result["response_shape"], request_shape=result["request_shape"],
                    validations=result["validations"], calls=result["calls"], evidence=evidence,
                ),
            )
            unit.status = "success"
            outcome.add(unit)
            ctx.regenerated_endpoints.add(key)
            ctx.progress.unit_finished(ctx.name, label, "ok")

        ctx.endpoint_inventory_changed = bool(existing_keys - keep_api_keys)
        ctx.knowledge_writer.prune_endpoints(ctx.service_id, keep_api_keys)
        return outcome


class ComponentGenerator:
    """One class/controller/module per group of endpoints, composed from the endpoint
    summaries EndpointGenerator just wrote — never a fresh read of the group's raw code."""

    kind = "component"

    def run(self, ctx: IndexContext) -> UnitOutcome:
        outcome = UnitOutcome()
        api_summaries = ctx.knowledge_reader.api_summaries(ctx.service_id)
        previous_components = (
            {(item.name, item.file_path): item for item in ctx.knowledge_reader.component_summaries(ctx.service_id)}
            if not ctx.is_new and not ctx.force else {}
        )
        keep_component_keys: set[tuple[str, str]] = set()
        for component_name, group in ctx.component_groups.items():
            component_file = group[0].excerpt.file_path
            unit = IndexUnit(self.kind, (component_name, component_file))
            keep_component_keys.add((component_name, component_file))
            group_dep_files: set[str] = set()
            for endpoint in group:
                group_dep_files |= endpoint.dependency_files()
            retry_failed = index_runs_repo.last_run_unit_failed(
                ctx.conn, ctx.service_id, self.kind, unit.identity,
            )
            routes = [(endpoint.method, endpoint.path) for endpoint in group]
            endpoint_context = compose_endpoint_context(routes, api_summaries)
            prompt = _render_component_prompt(ctx.name, component_name, component_file, endpoint_context.text)
            schema = load_schema("component")
            input_digest = component_input_digest(getattr(ctx.backend, "cache_identity", None), prompt, schema)
            previous_component = previous_components.get((component_name, component_file))
            needs_regen = (
                ctx.force or ctx.is_new or bool(group_dep_files & ctx.changed)
                or any((endpoint.method, endpoint.path) in ctx.regenerated_endpoints for endpoint in group)
                or retry_failed
                or bool(
                    input_digest and previous_component and previous_component.input_digest
                    and previous_component.input_digest != input_digest
                )
            )
            label = f"component {component_name}"
            if not needs_regen:
                outcome.add(unit)
                ctx.progress.unit_finished(ctx.name, label, "skipped")
                continue
            ctx.progress.unit_started(ctx.name, label)
            if not retry_failed and input_digest is not None and previous_component is not None and (
                previous_component.input_digest == input_digest
            ):
                ctx.knowledge_writer.save_component(
                    ctx.service_id,
                    ComponentDocumentation(
                        component_name, component_file, previous_component.summary, endpoint_context.evidence,
                        input_digest,
                    ),
                )
                outcome.add(unit)
                ctx.progress.unit_finished(ctx.name, label, "skipped")
                continue
            generation = generate_with_retry(
                ctx.backend, prompt, schema, ctx.root, ctx.failures_root,
                f"{ctx.name}-component-{component_name}",
                on_attempt=lambda unit=unit: ctx.record_llm_invocation(unit),
                on_usage=lambda usage, unit=unit: ctx.record_usage(unit, usage),
                on_duration_ms=unit.record_duration,
                on_prompt=unit.record_prompt,
            )
            if not generation:
                unit.status = "failed"
                outcome.add(unit)
                ctx.progress.unit_finished(ctx.name, label, "failed")
                continue
            result = generation.structured
            ctx.knowledge_writer.save_component(
                ctx.service_id,
                ComponentDocumentation(
                    component_name, component_file, result["summary"], endpoint_context.evidence, input_digest,
                ),
            )
            unit.status = "success"
            outcome.add(unit)
            ctx.any_component_regenerated = True
            ctx.progress.unit_finished(ctx.name, label, "ok")

        ctx.knowledge_writer.prune_components(ctx.service_id, keep_component_keys)
        return outcome


class PersistenceGenerator:
    kind = "persistence"

    def run(self, ctx: IndexContext) -> UnitOutcome:
        outcome = UnitOutcome()
        persistence_files = {p.excerpt.file_path for p in ctx.hints.persistence}
        if not ctx.hints.persistence:
            ctx.knowledge_writer.replace_persistence(ctx.service_id, PersistenceDocumentation([], []))
            return outcome

        unit = IndexUnit(self.kind, ())
        if not (
            ctx.generation_policy.should_regenerate_aggregate(persistence_files)
            or index_runs_repo.last_run_unit_failed(ctx.conn, ctx.service_id, self.kind, unit.identity)
        ):
            outcome.add(unit)
            ctx.progress.unit_finished(ctx.name, "persistence", "skipped")
            return outcome

        ctx.progress.unit_started(ctx.name, "persistence")
        persistence_config_excerpts = (
            collect_config_excerpts(ctx.root) if _persistence_needs_config_evidence(ctx.hints) else []
        )
        source = AggregateEvidenceSource.from_groups(
            [p.excerpt for p in ctx.hints.persistence], persistence_config_excerpts, MAX_EXCERPT_CHARS,
        )
        prompt = _render_persistence_prompt(
            ctx.name, ctx.detector.id, ctx.hints, persistence_config_excerpts, source,
        )
        generation = generate_with_retry(
            ctx.backend, prompt, load_schema("persistence"), ctx.root, ctx.failures_root, f"{ctx.name}-persistence",
            on_attempt=lambda: ctx.record_llm_invocation(unit),
            on_usage=lambda usage: ctx.record_usage(unit, usage),
            on_duration_ms=unit.record_duration,
            on_prompt=unit.record_prompt,
        )
        if generation:
            result = generation.structured
            entities = [
                PersistenceEntity(e["name"], e["kind"], e["engine"], e["fields"])
                for e in result["entities"]
            ]
            evidence = source.pointers
            ctx.knowledge_writer.replace_persistence(ctx.service_id, PersistenceDocumentation(entities, evidence))
            unit.status = "success"
            outcome.add(unit)
            ctx.progress.unit_finished(ctx.name, "persistence", "ok")
        else:
            unit.status = "failed"
            outcome.add(unit)
            ctx.progress.unit_finished(ctx.name, "persistence", "failed")
        return outcome


class MessagingGenerator:
    kind = "messaging"

    def run(self, ctx: IndexContext) -> UnitOutcome:
        outcome = UnitOutcome()
        messaging_files = {m.excerpt.file_path for m in ctx.hints.messaging}
        if not ctx.hints.messaging:
            ctx.knowledge_writer.replace_messaging(ctx.service_id, MessagingDocumentation([], []))
            return outcome

        unit = IndexUnit(self.kind, ())
        if not (
            ctx.generation_policy.should_regenerate_aggregate(messaging_files)
            or index_runs_repo.last_run_unit_failed(ctx.conn, ctx.service_id, self.kind, unit.identity)
        ):
            outcome.add(unit)
            ctx.progress.unit_finished(ctx.name, "messaging", "skipped")
            return outcome

        ctx.progress.unit_started(ctx.name, "messaging")
        config_excerpts = collect_config_excerpts(ctx.root) if _needs_config_evidence(ctx.hints) else []
        source = AggregateEvidenceSource.from_groups(
            [m.excerpt for m in ctx.hints.messaging], config_excerpts, MAX_EXCERPT_CHARS,
        )
        prompt = _render_messaging_prompt(ctx.name, ctx.detector.id, ctx.hints, config_excerpts, source)
        generation = generate_with_retry(
            ctx.backend, prompt, load_schema("messaging"), ctx.root, ctx.failures_root, f"{ctx.name}-messaging",
            on_attempt=lambda: ctx.record_llm_invocation(unit),
            on_usage=lambda usage: ctx.record_usage(unit, usage),
            on_duration_ms=unit.record_duration,
            on_prompt=unit.record_prompt,
        )
        if generation:
            result = generation.structured
            messages = [
                MessageDocumentation(m["direction"], m["channel"], m["provider"], m["shape"], m["description"])
                for m in result["messages"]
            ]
            evidence = source.pointers
            ctx.knowledge_writer.replace_messaging(ctx.service_id, MessagingDocumentation(messages, evidence))
            unit.status = "success"
            outcome.add(unit)
            ctx.progress.unit_finished(ctx.name, "messaging", "ok")
        else:
            unit.status = "failed"
            outcome.add(unit)
            ctx.progress.unit_finished(ctx.name, "messaging", "failed")
        return outcome


class OverviewGenerator:
    """Composed LAST, from the components' own summaries above (see the docstring on
    _render_service_overview_prompt) — never a fresh read of the entrypoint alone."""

    kind = "overview"

    def run(self, ctx: IndexContext) -> UnitOutcome:
        outcome = UnitOutcome()
        unit = IndexUnit(self.kind, ())
        entry_files = {ctx.hints.entry_excerpt.file_path} if ctx.hints.entry_excerpt else set()
        needs_overview = (
            ctx.force or ctx.is_new or bool(ctx.changed & entry_files)
            or not (ctx.existing and ctx.existing["short_desc"])
            or ctx.endpoint_inventory_changed or ctx.any_component_regenerated
            or index_runs_repo.last_run_unit_failed(ctx.conn, ctx.service_id, self.kind, unit.identity)
        )
        if not needs_overview:
            outcome.add(unit)
            ctx.progress.unit_finished(ctx.name, "overview", "skipped")
            return outcome

        ctx.progress.unit_started(ctx.name, "overview")
        components = ctx.knowledge_reader.component_summaries(ctx.service_id)
        prompt = _render_service_overview_prompt(ctx.name, ctx.detector.id, ctx.root, ctx.hints, components)
        generation = generate_with_retry(
            ctx.backend, prompt, load_schema("service_overview"), ctx.root, ctx.failures_root, f"{ctx.name}-overview",
            on_attempt=lambda: ctx.record_llm_invocation(unit),
            on_usage=lambda usage: ctx.record_usage(unit, usage),
            on_duration_ms=unit.record_duration,
            on_prompt=unit.record_prompt,
        )
        if generation:
            result = generation.structured
            ctx.knowledge_writer.save_overview(
                ctx.service_id, OverviewDocumentation(result["short_desc"], result["long_desc"]),
            )
            unit.status = "success"
            outcome.add(unit)
            self._update_embedding(ctx, result["short_desc"], result["long_desc"])
            ctx.progress.unit_finished(ctx.name, "overview", "ok")
        else:
            unit.status = "failed"
            outcome.add(unit)
            ctx.progress.unit_finished(ctx.name, "overview", "failed")
        return outcome

    @staticmethod
    def _update_embedding(ctx: IndexContext, short_desc: str, long_desc: str) -> None:
        """Zero-LLM-cost: only runs when an embedding_backend was actually injected
        (i.e. the `semantic` extra is installed — see
        generation.embeddings.try_create_default_backend, wired in by the CLI), and
        only on the same trigger as the overview regen itself, never as a separate
        indexing pass."""
        if ctx.embedding_backend is None:
            return
        vector = ctx.embedding_backend.embed([f"{short_desc} {long_desc}"])[0]
        embeddings_repo.upsert_service_embedding(ctx.conn, ctx.service_id, ctx.embedding_backend.model_name, vector)


UNIT_GENERATORS: tuple[UnitGenerator, ...] = (
    EndpointGenerator(), ComponentGenerator(), PersistenceGenerator(), MessagingGenerator(), OverviewGenerator(),
)


def _index_service_unlocked(
    conn: sqlite3.Connection,
    name: str,
    root: Path,
    detector: StackDetector,
    backend: LLMBackend,
    force: bool = False,
    failures_root: Path | None = None,
    progress: ProgressReporter | None = None,
    repository_id: int | None = None,
    embedding_backend: EmbeddingBackend | None = None,
    depth_provider: DepthProvider | None = None,
    knowledge_reader: KnowledgeReader | None = None,
    knowledge_writer: KnowledgeWriter | None = None,
    budget: ModelBudget | None = None,
    analysis_engine: StaticAnalysisEngine | None = None,
) -> IndexResult:
    failures_root = failures_root or (Path.home() / ".orbitkb" / "failures")
    progress = progress or NullProgressReporter()
    logger.debug("collecting hints: %s (stack=%s) at %s", name, detector.id, root)
    hints = detector.collect_hints(root)
    component_groups = _group_endpoints_by_component(hints.endpoints)
    hints.endpoints = _unique_endpoint_routes(hints.endpoints)
    total_units = (
        1 + len(hints.endpoints) + len(component_groups)
        + (1 if hints.persistence else 0) + (1 if hints.messaging else 0)
    )
    progress.service_started(name, total_units)

    existing = services_repo.get_service_by_name(conn, name, repository_id=repository_id)
    if existing is None:
        existing = services_repo.get_service_by_root_path(conn, str(root), repository_id)
    is_new = existing is None
    service_id = services_repo.ensure_service(conn, name, str(root), detector.id, repository_id=repository_id)
    static_engine = analysis_engine or StaticAnalysisEngine(depth_provider or NoopDepthProvider())
    cacheable_static_analysis = analysis_engine is None and (
        depth_provider is None or isinstance(depth_provider, NoopDepthProvider)
    )
    static_digest = static_engine.input_digest(root, detector.id) if cacheable_static_analysis else None
    snapshot = static_analysis_repo.get_snapshot(conn, service_id)
    static_analysis_is_current = (
        cacheable_static_analysis and not force and static_digest is not None and snapshot is not None
        and snapshot["input_digest"] == static_digest
        and snapshot["analysis_version"] == STATIC_ANALYSIS_INPUT_VERSION
        and canonical_snapshots_repo.has_snapshot(conn, service_id)
    )
    previous_snapshot = (
        canonical_snapshots_repo.read_snapshot(conn, service_id)
        if not static_analysis_is_current and canonical_snapshots_repo.has_snapshot(conn, service_id)
        else None
    )
    if not static_analysis_is_current:
        if not cacheable_static_analysis:
            static_analysis_repo.delete_snapshot(conn, service_id)
        analysis = static_engine.analyze(root, detector.id)
        flows_repo.replace_analysis(conn, service_id, analysis)
        if cacheable_static_analysis and static_digest is not None and static_engine.input_digest(root, detector.id) == static_digest:
            static_analysis_repo.replace_snapshot(
                conn, service_id, static_digest, STATIC_ANALYSIS_INPUT_VERSION,
            )
    security_findings_repo.replace_findings(conn, service_id, find_security_findings(root))

    old_hashes = indexed_files_repo.get_indexed_file_hashes(conn, service_id)
    route_sources: set[str] = set()
    current_snapshot = canonical_snapshots_repo.read_snapshot(conn, service_id)
    source_units_by_route: dict[tuple[str, str], tuple[SourceUnit, ...]] = {}
    current_unit_digests: dict[str, tuple[str, str]] = {}
    if current_snapshot is not None:
        source_collector = SourceUnitCollector(current_snapshot, root)
        for endpoint in hints.endpoints:
            key = (endpoint.method, endpoint.path)
            units = source_collector.for_route(*key)
            source_units_by_route[key] = units
            for unit in units:
                current_unit_digests[unit.unit_key] = (unit.fact_id, unit.digest)
                route_sources.add(unit.excerpt.file_path)
    previous_unit_digests = source_units_repo.read_digests(conn, service_id)
    changed_source_unit_keys = {
        unit_key for unit_key, (_, digest) in current_unit_digests.items()
        if previous_unit_digests.get(unit_key) != digest
    }
    for endpoint in hints.endpoints:
        for route_snapshot in (previous_snapshot, current_snapshot):
            if route_snapshot is None:
                continue
            capsule = route_capsule(route_snapshot, endpoint.method, endpoint.path)
            if capsule is None:
                continue
            route_sources.update(
                source.file_path
                for fact in capsule.facts if fact.kind in {"entrypoint", "security_requirement"}
                for source in fact.sources
            )
    relevant = hints.relevant_files() | route_sources
    new_hashes: dict[str, str] = {}
    changed: set[str] = set()
    for rel in relevant:
        path = root / rel
        if not path.is_file():
            continue
        h = file_hash(path)
        new_hashes[rel] = h
        if force or old_hashes.get(rel) != h:
            changed.add(rel)
    stale_hashes = set(old_hashes) - set(new_hashes)
    removed = {rel for rel in stale_hashes if not (root / rel).is_file()}

    index_runs_repo.recover_unfinished_runs(conn, service_id)
    run_id = index_runs_repo.start_index_run(conn, service_id, backend.name)

    knowledge_adapter = LegacyKnowledgeAdapter(conn)
    ctx = IndexContext(
        conn=conn, name=name, root=root, detector=detector, backend=backend, hints=hints,
        component_groups=component_groups, service_id=service_id, is_new=is_new, existing=existing,
        changed=changed, removed=removed, force=force, failures_root=failures_root, progress=progress,
        knowledge_reader=knowledge_reader if knowledge_reader is not None else knowledge_adapter,
        knowledge_writer=knowledge_writer if knowledge_writer is not None else knowledge_adapter,
        generation_policy=GenerationPolicy(force, is_new, changed, removed),
        previous_snapshot=previous_snapshot,
        budget=budget or ModelBudget(),
        source_units_by_route=source_units_by_route,
        changed_source_unit_keys=changed_source_unit_keys,
        embedding_backend=embedding_backend,
    )

    llm_calls = 0
    had_failure = False
    total_usage = LLMUsage()
    for generator in UNIT_GENERATORS:
        invocations_before = ctx.llm_invocations
        outcome = generator.run(ctx)
        for unit in outcome.units:
            index_runs_repo.record_run_unit(
                conn, run_id, service_id, unit.kind, unit.identity, unit.status,
                unit.llm_invocations, unit.usage.input_tokens, unit.usage.output_tokens,
                unit.usage.cost_usd, unit.backend_duration_ms, unit.usage.cached_input_tokens,
                unit.prompt_chars,
            )
        index_runs_repo.record_unit_usage(
            conn, run_id, generator.kind, outcome.generated_units,
            ctx.llm_invocations - invocations_before, outcome.had_failure,
            outcome.usage.input_tokens, outcome.usage.output_tokens, outcome.usage.cost_usd,
            outcome.backend_duration_ms,
        )
        llm_calls += outcome.llm_calls
        had_failure = had_failure or outcome.had_failure
        total_usage = total_usage + outcome.usage

    route_files = {e.excerpt.file_path for e in hints.endpoints}
    persistence_files = {p.excerpt.file_path for p in hints.persistence}
    messaging_files = {m.excerpt.file_path for m in hints.messaging}
    for rel, h in new_hashes.items():
        category = "route" if rel in route_files else (
            "persistence" if rel in persistence_files else ("messaging" if rel in messaging_files else "other")
        )
        indexed_files_repo.set_indexed_file_hash(conn, service_id, rel, h, category)
    if stale_hashes:
        indexed_files_repo.remove_indexed_files(conn, service_id, stale_hashes)
    source_units_repo.replace_digests(conn, service_id, current_unit_digests)
    conn.commit()

    services_repo.set_service_last_commit(conn, service_id, git_head_commit(root))
    service_calls_repo.reconcile_service_call_targets(conn)
    search_repo.rebuild_search_index_for_service(conn, service_id)
    recompute_architecture_view(conn)

    status = "partial" if had_failure else "ok"
    run_error = (
        f"LLM {ctx.budget.stop_reason}; pending units will retry on the next run"
        if ctx.budget.stop_reason else "some units failed, see failures dir" if had_failure else None
    )
    index_runs_repo.finish_index_run(
        conn, run_id, status, len(changed) + len(removed), llm_calls,
        run_error,
        input_tokens=total_usage.input_tokens, output_tokens=total_usage.output_tokens, cost_usd=total_usage.cost_usd,
        llm_invocations=ctx.llm_invocations,
    )
    progress.service_finished(name)

    return IndexResult(
        service_name=name, service_id=service_id,
        files_changed=len(changed) + len(removed), llm_calls=llm_calls, status=status,
        llm_invocations=ctx.llm_invocations,
        input_tokens=total_usage.input_tokens, output_tokens=total_usage.output_tokens, cost_usd=total_usage.cost_usd,
        sufficiency_shadow=ctx.sufficiency_shadow,
        sufficiency_details=tuple(ctx.sufficiency_details),
    )


def index_service(
    conn: sqlite3.Connection, name: str, root: Path, detector: StackDetector, backend: LLMBackend,
    force: bool = False, failures_root: Path | None = None, progress: ProgressReporter | None = None,
    repository_id: int | None = None, embedding_backend: EmbeddingBackend | None = None,
    depth_provider: DepthProvider | None = None,
    knowledge_reader: KnowledgeReader | None = None,
    knowledge_writer: KnowledgeWriter | None = None,
    max_llm_invocations: int | None = None,
    max_reported_cost_usd: float | None = None,
    max_reported_tokens: int | None = None,
    analysis_engine: StaticAnalysisEngine | None = None,
) -> IndexResult:
    """Serialize one service identity while retaining independent-service parallelism.

    An explicit analysis engine is recomputed each run because its frontend
    implementation is not identified by the default source digest.
    """
    if analysis_engine is not None and depth_provider is not None:
        raise ValueError("analysis_engine and depth_provider cannot both be supplied")
    budget = ModelBudget(
        max_invocations=max_llm_invocations,
        max_reported_cost_usd=max_reported_cost_usd,
        max_reported_tokens=max_reported_tokens,
    )
    lock_key = f"{repository_id if repository_id is not None else 'standalone'}:{name}"
    if not index_runs_repo.acquire_service_lock(conn, lock_key):
        raise RuntimeError(f"index already in progress for service {name!r}")
    try:
        return _index_service_unlocked(
            conn, name, root, detector, backend, force, failures_root, progress,
            repository_id, embedding_backend, depth_provider, knowledge_reader, knowledge_writer,
            budget, analysis_engine,
        )
    finally:
        index_runs_repo.release_service_lock(conn, lock_key)


# ---------------------------------------------------------------------------
# entry point used by the CLI
# ---------------------------------------------------------------------------

def index_path(
    conn: sqlite3.Connection,
    path: Path,
    backend: LLMBackend,
    service_override: str | None = None,
    force: bool = False,
    progress: ProgressReporter | None = None,
    repository_name: str | None = None,
    embedding_backend: EmbeddingBackend | None = None,
    stack_override: str | None = None,
    depth_provider: DepthProvider | None = None,
    max_llm_invocations: int | None = None,
    max_reported_cost_usd: float | None = None,
    max_reported_tokens: int | None = None,
) -> list[IndexResult]:
    if stack_override is not None:
        # Explicit "I already know what this is" escape hatch (see `orbitkb index
        # --stack`): skips discover_services()/matches() entirely, for a folder shape
        # no heuristic recognizes (e.g. a library/CLI package with its manifest at
        # the repo root and source in a subdirectory — see README's known limitation
        # this replaces). Broadening matches() itself would reduce detection
        # precision for real web-service targets, so this stays an explicit,
        # single-service opt-in rather than a heuristic change.
        if not service_override:
            raise DiscoveryError("--stack requires --service (both must name one explicit service)")
        detector = detector_by_id(stack_override)
        if detector is None:
            raise DiscoveryError(f"unknown --stack {stack_override!r}")
        candidates = [ServiceCandidate(name=service_override, path=path.resolve(), detector=detector)]
    else:
        candidates = discover_services(path)
        if not candidates:
            raise DiscoveryError(
                f"No supported microservice detected under {path} "
                "(looked for Node/TS, Python, JVM/Spring, and Go boundary markers)."
            )
        if service_override:
            if len(candidates) != 1:
                raise DiscoveryError("--service can only be used when <path> points at a single service")
            candidates = [replace(candidates[0], name=service_override)]

    resolved_path = path.resolve()
    repository_id = repositories_repo.ensure_repository(conn, repository_name or resolved_path.name, str(resolved_path))

    results = [
        index_service(
            conn, c.name, c.path, c.detector, backend, force=force, progress=progress,
            repository_id=repository_id, embedding_backend=embedding_backend, depth_provider=depth_provider,
            max_llm_invocations=max_llm_invocations,
            max_reported_cost_usd=max_reported_cost_usd,
            max_reported_tokens=max_reported_tokens,
        )
        for c in candidates
    ]
    removed_service_ids = services_repo.delete_services_not_in(conn, repository_id, {candidate.name for candidate in candidates})
    if removed_service_ids:
        service_calls_repo.reconcile_service_call_targets(conn)
        search_repo.rebuild_search_index(conn)

    # Scanned once per repository, after every candidate service has a real row
    # (and therefore a real service_id to attribute a matched resource to) —
    # IaC commonly lives outside any single service's own root, so this can't
    # be folded into index_service's per-service pass.
    iac_facts = scan_repository_facts(resolved_path, candidates)
    ci_commands_repo.replace_ci_commands(conn, repository_id, scan_github_actions_commands(resolved_path))
    cloud_iac_repo.replace_iac_resources(conn, repository_id, iac_facts.resources)
    kubernetes_configuration_repo.replace_kubernetes_configuration_bindings(
        conn, repository_id, iac_facts.configuration_bindings,
    )
    kubernetes_configuration_repo.replace_kubernetes_configuration_source_imports(
        conn, repository_id, iac_facts.configuration_source_imports,
    )
    kubernetes_configuration_repo.replace_kubernetes_configuration_source_import_unknowns(
        conn, repository_id, iac_facts.configuration_source_import_unknowns,
    )
    kubernetes_configuration_repo.replace_kubernetes_configuration_key_mismatches(
        conn, repository_id, iac_facts.configuration_key_mismatches,
    )
    kubernetes_configuration_repo.replace_kubernetes_configuration_source_unknowns(
        conn, repository_id, iac_facts.configuration_source_unknowns,
    )
    # index_service's own recompute_architecture_view call (above, per service)
    # necessarily runs *before* this repository's IaC scan on a fresh index —
    # cloud_iac_resources findings computed there would be stale by exactly one
    # index cycle otherwise. Recomputing once more, unconditionally, here
    # guarantees every finding reflects both this run's code facts and this
    # run's IaC facts by the time index_path returns.
    recompute_architecture_view(conn)
    return results
