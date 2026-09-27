from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from orbitkb.analysis.depth import DepthProvider, NoopDepthProvider
from orbitkb.analysis.engine import STATIC_ANALYSIS_INPUT_VERSION, StaticAnalysisEngine
from orbitkb.analysis.models import AnalysisResult
from orbitkb.ci.scanner import scan_github_actions_commands
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import ci_commands as ci_commands_repo
from orbitkb.db.repositories import cloud_iac as cloud_iac_repo
from orbitkb.db.repositories import components as components_repo
from orbitkb.db.repositories import embeddings as embeddings_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import index_runs as index_runs_repo
from orbitkb.db.repositories import indexed_files as indexed_files_repo
from orbitkb.db.repositories import (
    kubernetes_configuration as kubernetes_configuration_repo,
)
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.db.repositories import search as search_repo
from orbitkb.db.repositories import security_findings as security_findings_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.db.repositories import static_analysis as static_analysis_repo
from orbitkb.discovery.base import (
    CodeExcerpt,
    EndpointHint,
    ServiceHints,
    StackDetector,
)
from orbitkb.discovery.hashing import file_hash, git_head_commit
from orbitkb.discovery.isolation import run_isolated
from orbitkb.discovery.registry import detector_by_id
from orbitkb.discovery.scan_helpers import SKIP_DIRS, collect_config_excerpts
from orbitkb.discovery.walker import ServiceCandidate, discover_services
from orbitkb.generation.architecture import recompute_architecture_view
from orbitkb.generation.backend_base import LLMBackend, LLMUsage
from orbitkb.generation.embeddings import EmbeddingBackend
from orbitkb.generation.llm_harness import generate_with_retry, load_prompt, load_schema
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
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


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

def _join_excerpts(excerpts: list[CodeExcerpt], max_chars: int = MAX_EXCERPT_CHARS) -> str:
    parts: list[str] = []
    total = 0
    for e in excerpts:
        block = f"--- {e.file_path} (lines {e.start_line}-{e.end_line}) ---\n{redact_sensitive_values(e.text)}\n"
        if total + len(block) > max_chars:
            parts.append("... (truncated, excerpt budget reached)")
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts) if parts else "(no excerpts found)"


def _evidence_from_excerpts(excerpts: list[CodeExcerpt]) -> list[dict]:
    """Turn discovery excerpts into persistable evidence pointers (file + line range).

    This is the evidence an LLM call actually saw when it produced a claim, so it's
    attached as-is to whatever that call generated — it is never fabricated beyond
    what discovery already found.
    """
    return [{"file": e.file_path, "start_line": e.start_line, "end_line": e.end_line} for e in excerpts]


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
    name: str, stack: str, root: Path, hints: ServiceHints, components: list[sqlite3.Row]
) -> str:
    """Composed LAST in a service's generation run, from the components' own already-
    written summaries — never a fresh read of the entrypoint alone — so the overview
    reflects what the endpoints/classes actually turned out to do, not a guess made
    before any of them were analyzed."""
    entry_file = hints.entry_excerpt.file_path if hints.entry_excerpt else "(none found)"
    entry_excerpt = redact_sensitive_values(hints.entry_excerpt.text) if hints.entry_excerpt else "(no entrypoint file detected)"
    component_summaries = "\n".join(f"- {c['name']} ({c['file_path']}): {c['summary']}" for c in components) or (
        "(no classes/controllers detected — this service's routing is likely function-based, "
        "or it exposes no HTTP endpoints at all)"
    )
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


def _render_api_detail_prompt(name: str, stack: str, endpoint: EndpointHint, hints: ServiceHints) -> str:
    excerpts = [endpoint.excerpt, *endpoint.extra_excerpts]
    code = _join_excerpts(excerpts)
    dep_files = endpoint.dependency_files()
    own_calls = [c for c in hints.outbound_calls if c.excerpt.file_path in dep_files]
    outbound = "\n".join(
        f"- [{c.call_kind}] {c.target_hint} ({c.excerpt.file_path}:{c.excerpt.start_line})"
        for c in own_calls[:30]
    ) or "(none found)"
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


def _render_persistence_prompt(name: str, stack: str, hints: ServiceHints, config_excerpts: list[CodeExcerpt]) -> str:
    excerpts = [p.excerpt for p in hints.persistence]
    engine_hints = "\n".join(
        f"- {p.name_hint} ({p.excerpt.file_path}:{p.excerpt.start_line}): {p.engine_hint or 'unknown'}"
        for p in hints.persistence
    ) or "(none found)"
    config_evidence = _join_excerpts(config_excerpts) if config_excerpts else "(none found)"
    return load_prompt("persistence").substitute(
        service_name=name, stack=stack, persistence_excerpts=_join_excerpts(excerpts),
        engine_hints=engine_hints, config_evidence=config_evidence,
    )


def _needs_config_evidence(hints: ServiceHints) -> bool:
    """A messaging hint tagged 'abstracted' matched a transport-agnostic API (JMS,
    Celery, NestJS microservices) whose concrete broker only lives in configuration."""
    return any(m.provider_hint == "abstracted" for m in hints.messaging)


def _render_messaging_prompt(name: str, stack: str, hints: ServiceHints, config_excerpts: list[CodeExcerpt]) -> str:
    excerpts = [m.excerpt for m in hints.messaging]
    provider_hints = "\n".join(
        f"- {m.direction} {m.channel_hint} ({m.excerpt.file_path}:{m.excerpt.start_line}): "
        f"{m.provider_hint or 'unknown'}"
        for m in hints.messaging
    ) or "(none found)"
    config_evidence = _join_excerpts(config_excerpts) if config_excerpts else "(none found)"
    return load_prompt("messaging").substitute(
        service_name=name, stack=stack, messaging_excerpts=_join_excerpts(excerpts),
        provider_hints=provider_hints, config_evidence=config_evidence,
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
    """What one UnitGenerator.run() call did: how many LLM calls it spent, whether
    any of its sub-units failed, and which files those failures touched (so their
    hash is deliberately NOT persisted afterward — see index_service's final loop —
    and the unit is retried next run instead of being silently skipped forever)."""

    llm_calls: int = 0
    had_failure: bool = False
    failed_files: set[str] = field(default_factory=set)
    usage: LLMUsage = field(default_factory=LLMUsage)


@dataclass
class IndexContext:
    """Shared state one index_service call passes through every UnitGenerator.
    any_endpoint_regenerated/any_component_regenerated are written by the endpoint/
    component generators and read by OverviewGenerator (composed last, see above)."""

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
    embedding_backend: EmbeddingBackend | None = None
    any_endpoint_regenerated: bool = False
    any_component_regenerated: bool = False


class UnitGenerator(Protocol):
    def run(self, ctx: IndexContext) -> UnitOutcome: ...


class EndpointGenerator:
    """Finest-grained unit, generated first so components/overview can compose from
    its summaries (see the module-level docstring above)."""

    def run(self, ctx: IndexContext) -> UnitOutcome:
        outcome = UnitOutcome()
        keep_api_keys: set[tuple[str, str]] = set()
        for endpoint in ctx.hints.endpoints:
            key = (endpoint.method, endpoint.path)
            keep_api_keys.add(key)
            existing_api = apis_repo.get_api_by_key(ctx.conn, ctx.service_id, *key)
            dep_files = endpoint.dependency_files()
            needs_regen = ctx.force or existing_api is None or bool(dep_files & ctx.changed)
            label = f"{endpoint.method} {endpoint.path}"
            if not needs_regen:
                ctx.progress.unit_finished(ctx.name, label, "skipped")
                continue
            ctx.progress.unit_started(ctx.name, label)
            prompt = _render_api_detail_prompt(ctx.name, ctx.detector.id, endpoint, ctx.hints)
            generation = generate_with_retry(
                ctx.backend, prompt, load_schema("api_detail"), ctx.root, ctx.failures_root,
                f"{ctx.name}-{endpoint.method}-{endpoint.path}",
            )
            if not generation:
                outcome.had_failure = True
                outcome.failed_files |= dep_files
                ctx.progress.unit_finished(ctx.name, label, "failed")
                continue
            result = generation.structured
            evidence = _evidence_from_excerpts([endpoint.excerpt, *endpoint.extra_excerpts])
            api_id = apis_repo.upsert_api(
                ctx.conn, ctx.service_id, endpoint.method, endpoint.path,
                result["summary"], result["description"], result["response_shape"], evidence,
                request_shape=result["request_shape"],
            )
            apis_repo.replace_api_validations(ctx.conn, api_id, result["validations"])
            service_calls_repo.replace_calls_for_api(ctx.conn, ctx.service_id, api_id, result["calls"], evidence)
            outcome.llm_calls += 1
            outcome.usage = outcome.usage + generation.usage
            ctx.any_endpoint_regenerated = True
            ctx.progress.unit_finished(ctx.name, label, "ok")

        apis_repo.prune_apis_not_in(ctx.conn, ctx.service_id, keep_api_keys)
        return outcome


class ComponentGenerator:
    """One class/controller/module per group of endpoints, composed from the endpoint
    summaries EndpointGenerator just wrote — never a fresh read of the group's raw code."""

    def run(self, ctx: IndexContext) -> UnitOutcome:
        outcome = UnitOutcome()
        current_apis_by_key = {
            (row["method"], row["path"]): row for row in apis_repo.list_apis(ctx.conn, ctx.service_id)
        }
        keep_component_keys: set[tuple[str, str]] = set()
        for component_name, group in ctx.component_groups.items():
            component_file = group[0].excerpt.file_path
            keep_component_keys.add((component_name, component_file))
            group_dep_files: set[str] = set()
            for endpoint in group:
                group_dep_files |= endpoint.dependency_files()
            needs_regen = ctx.force or ctx.is_new or bool(group_dep_files & ctx.changed)
            label = f"component {component_name}"
            if not needs_regen:
                ctx.progress.unit_finished(ctx.name, label, "skipped")
                continue
            ctx.progress.unit_started(ctx.name, label)
            summary_lines = []
            for endpoint in group:
                api_row = current_apis_by_key.get((endpoint.method, endpoint.path))
                if api_row is not None:
                    summary_lines.append(f"- {endpoint.method} {endpoint.path}: {api_row['summary']}")
            endpoint_summaries = "\n".join(summary_lines) or "(no endpoint summaries available yet)"
            prompt = _render_component_prompt(ctx.name, component_name, component_file, endpoint_summaries)
            generation = generate_with_retry(
                ctx.backend, prompt, load_schema("component"), ctx.root, ctx.failures_root,
                f"{ctx.name}-component-{component_name}",
            )
            if not generation:
                outcome.had_failure = True
                outcome.failed_files |= group_dep_files
                ctx.progress.unit_finished(ctx.name, label, "failed")
                continue
            result = generation.structured
            evidence = _evidence_from_excerpts([endpoint.excerpt for endpoint in group])
            components_repo.upsert_component(
                ctx.conn, ctx.service_id, component_name, component_file, result["summary"], evidence
            )
            outcome.llm_calls += 1
            outcome.usage = outcome.usage + generation.usage
            ctx.any_component_regenerated = True
            ctx.progress.unit_finished(ctx.name, label, "ok")

        components_repo.prune_components_not_in(ctx.conn, ctx.service_id, keep_component_keys)
        return outcome


class PersistenceGenerator:
    def run(self, ctx: IndexContext) -> UnitOutcome:
        outcome = UnitOutcome()
        persistence_files = {p.excerpt.file_path for p in ctx.hints.persistence}
        if not ctx.hints.persistence:
            persistence_repo.replace_persistence_entities(ctx.conn, ctx.service_id, [], [])
            return outcome

        if not (ctx.force or ctx.is_new or (ctx.changed & persistence_files) or (ctx.removed & persistence_files)):
            ctx.progress.unit_finished(ctx.name, "persistence", "skipped")
            return outcome

        ctx.progress.unit_started(ctx.name, "persistence")
        persistence_config_excerpts = (
            collect_config_excerpts(ctx.root) if _persistence_needs_config_evidence(ctx.hints) else []
        )
        prompt = _render_persistence_prompt(ctx.name, ctx.detector.id, ctx.hints, persistence_config_excerpts)
        generation = generate_with_retry(
            ctx.backend, prompt, load_schema("persistence"), ctx.root, ctx.failures_root, f"{ctx.name}-persistence"
        )
        if generation:
            result = generation.structured
            entities = [
                {"name": e["name"], "kind": e["kind"], "engine": e["engine"], "schema_json": e["fields"]}
                for e in result["entities"]
            ]
            evidence = _evidence_from_excerpts([p.excerpt for p in ctx.hints.persistence] + persistence_config_excerpts)
            persistence_repo.replace_persistence_entities(ctx.conn, ctx.service_id, entities, evidence)
            outcome.llm_calls += 1
            outcome.usage = outcome.usage + generation.usage
            ctx.progress.unit_finished(ctx.name, "persistence", "ok")
        else:
            outcome.had_failure = True
            outcome.failed_files |= persistence_files
            ctx.progress.unit_finished(ctx.name, "persistence", "failed")
        return outcome


class MessagingGenerator:
    def run(self, ctx: IndexContext) -> UnitOutcome:
        outcome = UnitOutcome()
        messaging_files = {m.excerpt.file_path for m in ctx.hints.messaging}
        if not ctx.hints.messaging:
            messages_repo.replace_messages(ctx.conn, ctx.service_id, [], [])
            return outcome

        if not (ctx.force or ctx.is_new or (ctx.changed & messaging_files) or (ctx.removed & messaging_files)):
            ctx.progress.unit_finished(ctx.name, "messaging", "skipped")
            return outcome

        ctx.progress.unit_started(ctx.name, "messaging")
        config_excerpts = collect_config_excerpts(ctx.root) if _needs_config_evidence(ctx.hints) else []
        prompt = _render_messaging_prompt(ctx.name, ctx.detector.id, ctx.hints, config_excerpts)
        generation = generate_with_retry(
            ctx.backend, prompt, load_schema("messaging"), ctx.root, ctx.failures_root, f"{ctx.name}-messaging"
        )
        if generation:
            result = generation.structured
            messages = [
                {
                    "direction": m["direction"],
                    "channel": m["channel"],
                    "shape_json": m["shape"],
                    "description": m["description"],
                    "provider": m["provider"],
                }
                for m in result["messages"]
            ]
            evidence = _evidence_from_excerpts([m.excerpt for m in ctx.hints.messaging] + config_excerpts)
            messages_repo.replace_messages(ctx.conn, ctx.service_id, messages, evidence)
            outcome.llm_calls += 1
            outcome.usage = outcome.usage + generation.usage
            ctx.progress.unit_finished(ctx.name, "messaging", "ok")
        else:
            outcome.had_failure = True
            outcome.failed_files |= messaging_files
            ctx.progress.unit_finished(ctx.name, "messaging", "failed")
        return outcome


class OverviewGenerator:
    """Composed LAST, from the components' own summaries above (see the docstring on
    _render_service_overview_prompt) — never a fresh read of the entrypoint alone."""

    def run(self, ctx: IndexContext) -> UnitOutcome:
        outcome = UnitOutcome()
        entry_files = {ctx.hints.entry_excerpt.file_path} if ctx.hints.entry_excerpt else set()
        needs_overview = (
            ctx.force or ctx.is_new or bool(ctx.changed & entry_files)
            or not (ctx.existing and ctx.existing["short_desc"])
            or ctx.any_endpoint_regenerated or ctx.any_component_regenerated
        )
        if not needs_overview:
            ctx.progress.unit_finished(ctx.name, "overview", "skipped")
            return outcome

        ctx.progress.unit_started(ctx.name, "overview")
        components = components_repo.list_components(ctx.conn, ctx.service_id)
        prompt = _render_service_overview_prompt(ctx.name, ctx.detector.id, ctx.root, ctx.hints, components)
        generation = generate_with_retry(
            ctx.backend, prompt, load_schema("service_overview"), ctx.root, ctx.failures_root, f"{ctx.name}-overview"
        )
        if generation:
            result = generation.structured
            services_repo.update_service_overview(ctx.conn, ctx.service_id, result["short_desc"], result["long_desc"])
            outcome.llm_calls += 1
            outcome.usage = outcome.usage + generation.usage
            self._update_embedding(ctx, result["short_desc"], result["long_desc"])
            ctx.progress.unit_finished(ctx.name, "overview", "ok")
        else:
            outcome.had_failure = True
            outcome.failed_files |= entry_files
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


# Isolation costs a fresh subprocess spawn per call, so it's scoped to the stack with
# a proven native crash (tree-sitter-kotlin heap corruption, reproduced on a real
# service -- see orbitkb/discovery/isolation.py), not applied blanket to every stack.
_ISOLATED_STACKS = frozenset({"jvm-spring"})


def _collect_hints_isolated(root: Path, stack: str) -> ServiceHints:
    """`run_isolated()` target: builds its own detector rather than taking one as an
    argument, since a detector instance isn't guaranteed picklable and native state
    (a tree-sitter `Parser`) has to be constructed inside the subprocess anyway.
    """
    return detector_by_id(stack).collect_hints(root)


def _analyze_isolated(root: Path, stack: str) -> AnalysisResult:
    """`run_isolated()` target: builds its own engine (never pickled in) -- a
    `StaticAnalysisEngine` holds tree-sitter `Language` objects, which wrap native
    pointers and can't cross a process boundary.
    """
    return StaticAnalysisEngine(NoopDepthProvider()).analyze(root, stack)


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
) -> IndexResult:
    failures_root = failures_root or (Path.home() / ".orbitkb" / "failures")
    progress = progress or NullProgressReporter()
    logger.debug("collecting hints: %s (stack=%s) at %s", name, detector.id, root)
    hints_crash: str | None = None
    if detector.id in _ISOLATED_STACKS:
        hints, hints_crash = run_isolated(_collect_hints_isolated, root, detector.id)
        if hints_crash is not None:
            logger.warning(
                "hint collection %s for %s (stack=%s); continuing with no hints",
                hints_crash, name, detector.id,
            )
            hints = ServiceHints()
    else:
        hints = detector.collect_hints(root)
    component_groups = _group_endpoints_by_component(hints.endpoints)
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
    static_engine = StaticAnalysisEngine(depth_provider or NoopDepthProvider())
    static_digest = static_engine.input_digest(root, detector.id)
    snapshot = static_analysis_repo.get_snapshot(conn, service_id)
    cacheable_static_analysis = depth_provider is None or isinstance(depth_provider, NoopDepthProvider)
    static_analysis_is_current = (
        cacheable_static_analysis and not force and static_digest is not None and snapshot is not None
        and snapshot["input_digest"] == static_digest
        and snapshot["analysis_version"] == STATIC_ANALYSIS_INPUT_VERSION
    )
    analysis_crash: str | None = None
    if not static_analysis_is_current:
        if not cacheable_static_analysis:
            static_analysis_repo.delete_snapshot(conn, service_id)
            analysis = static_engine.analyze(root, detector.id)
        elif detector.id in _ISOLATED_STACKS:
            # Isolated only in the default (Noop) case: a real depth provider holds a
            # live external MCP connection that can't be handed to a fresh subprocess.
            analysis, analysis_crash = run_isolated(_analyze_isolated, root, detector.id)
            if analysis_crash is not None:
                logger.warning(
                    "static analysis %s for %s (stack=%s); continuing with no analysis",
                    analysis_crash, name, detector.id,
                )
                analysis = AnalysisResult()
        else:
            analysis = static_engine.analyze(root, detector.id)
        flows_repo.replace_analysis(conn, service_id, analysis)
        if cacheable_static_analysis and static_digest is not None:
            if static_engine.input_digest(root, detector.id) == static_digest:
                static_analysis_repo.replace_snapshot(
                    conn, service_id, static_digest, STATIC_ANALYSIS_INPUT_VERSION,
                )
    security_findings_repo.replace_findings(conn, service_id, find_security_findings(root))

    old_hashes = indexed_files_repo.get_indexed_file_hashes(conn, service_id)
    relevant = hints.relevant_files()
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
    removed = set(old_hashes) - set(new_hashes)

    index_runs_repo.recover_unfinished_runs(conn, service_id)
    run_id = index_runs_repo.start_index_run(conn, service_id, backend.name)

    ctx = IndexContext(
        conn=conn, name=name, root=root, detector=detector, backend=backend, hints=hints,
        component_groups=component_groups, service_id=service_id, is_new=is_new, existing=existing,
        changed=changed, removed=removed, force=force, failures_root=failures_root, progress=progress,
        embedding_backend=embedding_backend,
    )

    llm_calls = 0
    had_failure = hints_crash is not None or analysis_crash is not None
    # Files whose derived unit failed to generate this run. Their hash is deliberately
    # NOT persisted to indexed_files below, so next run sees them as "changed" again
    # and retries instead of silently skipping a permanently-broken unit forever.
    failed_files: set[str] = set()
    total_usage = LLMUsage()
    for generator in UNIT_GENERATORS:
        outcome = generator.run(ctx)
        llm_calls += outcome.llm_calls
        had_failure = had_failure or outcome.had_failure
        failed_files |= outcome.failed_files
        total_usage = total_usage + outcome.usage

    route_files = {e.excerpt.file_path for e in hints.endpoints}
    persistence_files = {p.excerpt.file_path for p in hints.persistence}
    messaging_files = {m.excerpt.file_path for m in hints.messaging}
    for rel, h in new_hashes.items():
        if rel in failed_files:
            continue  # retry these next run instead of locking in a broken generation forever
        category = "route" if rel in route_files else (
            "persistence" if rel in persistence_files else ("messaging" if rel in messaging_files else "other")
        )
        indexed_files_repo.set_indexed_file_hash(conn, service_id, rel, h, category)
    if removed:
        indexed_files_repo.remove_indexed_files(conn, service_id, removed)
    conn.commit()

    services_repo.set_service_last_commit(conn, service_id, git_head_commit(root))
    service_calls_repo.reconcile_service_call_targets(conn)
    search_repo.rebuild_search_index_for_service(conn, service_id)
    recompute_architecture_view(conn)

    status = "partial" if had_failure else "ok"
    if hints_crash is not None or analysis_crash is not None:
        run_error = f"hint collection {hints_crash or 'ok'}; static analysis {analysis_crash or 'ok'}"
    elif had_failure:
        run_error = "some units failed, see failures dir"
    else:
        run_error = None
    index_runs_repo.finish_index_run(
        conn, run_id, status, len(changed) + len(removed), llm_calls,
        run_error,
        input_tokens=total_usage.input_tokens, output_tokens=total_usage.output_tokens, cost_usd=total_usage.cost_usd,
    )
    progress.service_finished(name)

    return IndexResult(
        service_name=name, service_id=service_id,
        files_changed=len(changed) + len(removed), llm_calls=llm_calls, status=status,
        input_tokens=total_usage.input_tokens, output_tokens=total_usage.output_tokens, cost_usd=total_usage.cost_usd,
    )


def index_service(
    conn: sqlite3.Connection, name: str, root: Path, detector: StackDetector, backend: LLMBackend,
    force: bool = False, failures_root: Path | None = None, progress: ProgressReporter | None = None,
    repository_id: int | None = None, embedding_backend: EmbeddingBackend | None = None,
    depth_provider: DepthProvider | None = None,
) -> IndexResult:
    """Serialize one service identity while retaining independent-service parallelism."""
    lock_key = f"{repository_id if repository_id is not None else 'standalone'}:{name}"
    if not index_runs_repo.acquire_service_lock(conn, lock_key):
        raise RuntimeError(f"index already in progress for service {name!r}")
    try:
        return _index_service_unlocked(
            conn, name, root, detector, backend, force, failures_root, progress,
            repository_id, embedding_backend, depth_provider,
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
