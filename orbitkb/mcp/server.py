from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from orbitkb.config import resolve_backend
from orbitkb.db.connection import open_db
from orbitkb.generation.backend_base import LLMBackend
from orbitkb.mcp import queries
from orbitkb.mcp.activity import track_local_activity


def build_server(db_path: Path | None = None, backend: LLMBackend | None = None) -> MCPServer:
    mcp = MCPServer("orbitkb")
    # Only find_change_surface/get_change_context use a backend; the other tools are pure SQLite
    # reads and never touch it. Resolved once here rather than per-call since
    # constructing a backend is cheap (no subprocess runs until .generate() is called).
    resolved_backend = backend or resolve_backend(None)

    def _conn() -> sqlite3.Connection:
        # A fresh short-lived connection per call: tool handlers may run on different
        # worker threads, and sqlite3 connections aren't safe to share across threads.
        return open_db(db_path)

    @mcp.tool()
    def describe_indexing_capabilities() -> dict:
        """Return conservative static-analysis coverage for the initial stacks.

        Consult this before planning an unfamiliar stack or framework. Listed facts
        are deterministic; dynamic behavior and unlisted framework paths remain
        unknown rather than being inferred from source shape.
        """
        return queries.describe_indexing_capabilities()

    @mcp.tool()
    def list_repositories() -> dict:
        """Call this to see what's been cumulatively indexed so far, at the
        repository level — name, root path and how many services came from it.
        Knowledge here is additive: repositories can be indexed one at a time or as
        one monorepo, and this reflects everything indexed up to now, not just the
        last run. Next: list_services for the service-level view, or
        find_change_surface if you already have a specific engineering task."""
        with closing(_conn()) as conn:
            return queries.list_repositories(conn)

    @mcp.tool()
    def list_services(repository: str | None = None) -> dict:
        """Call this first, to see what's indexed: every microservice with a
        one-line description, repository, stack and API count. Pass repository to
        narrow a cumulative KB. Next: describe_service on
        whichever one is relevant, or find_change_surface if you have a specific
        engineering task rather than wanting a system overview."""
        with closing(_conn()) as conn:
            return queries.list_services(conn, repository)

    @mcp.tool()
    def describe_ci_commands(
        repository: str, limit: int = queries.DEFAULT_LIST_LIMIT, offset: int = 0,
    ) -> dict:
        """List safe literal test, build, migration and client-generation commands
        indexed from GitHub Actions workflows. Commands are evidence only: this tool
        never executes them. Dynamic, multiline and secret-bearing workflow steps are
        intentionally excluded. Use this before changing validation or delivery code."""
        with closing(_conn()) as conn:
            return queries.describe_ci_commands(conn, repository, limit, offset)

    @mcp.tool()
    def describe_service(
        service: str,
        limit: int = queries.DEFAULT_LIST_LIMIT,
        offset: int = 0,
        repository: str | None = None,
    ) -> dict:
        """Full picture of one microservice: description, why it calls other services/queues
        (with the business reason, data needed, and target_kind/resource_type when the
        target is external), unresolved source-proven HTTP targets and
        source_targets_status (unassessed/limited/assessed), its components
        (classes/controllers/modules, each with a summary), its APIs as one-liners,
        and what it persists/publishes by name only
        (use describe_persistence/describe_messages for the full field-level schema,
        including engine/provider). Includes freshness (indexed commit vs. the
        repository's current commit, and whether that means this knowledge may be
        stale). Every list (calls/source_targets/apis/components/persists/messages) is capped at
        `limit` items (default 50) starting at `offset`, so a service with dozens of
        endpoints can't blow your context budget by default — the `pagination` field
        reports each list's real total and whether it was truncated; raise `offset` by
        `limit` to fetch the next page of whichever list you need more of. Call this
        once you know which service is relevant. When duplicate service names exist,
        pass repository from list_services. Next: describe_api for a specific
        endpoint's contract, or get_relationships to see who else depends on it."""
        with closing(_conn()) as conn:
            return queries.describe_service(conn, service, limit, offset, repository)

    @mcp.tool()
    def describe_service_topology(
        service: str, repository: str | None = None, hops: int = queries.DEFAULT_TOPOLOGY_HOPS,
    ) -> dict:
        """A Mermaid graph TD of one service's own dependency neighborhood: who it
        calls, who calls it, its queues/message links, and its databases — scoped to
        `hops` steps out (default 1) rather than the whole indexed system's topology.
        Zero LLM cost: rendered straight from the same facts describe_service and
        get_relationships already list, just as a diagram. Raise hops for a wider
        view (e.g. 2 to also see what your direct dependencies themselves depend
        on). Pass repository when the service name is duplicated."""
        with closing(_conn()) as conn:
            return queries.describe_service_topology(conn, service, repository, hops)

    @mcp.tool()
    def list_apis(
        service: str, limit: int = queries.DEFAULT_LIST_LIMIT, offset: int = 0, repository: str | None = None,
    ) -> dict:
        """One-line summary of every API on one microservice — a thinner view than
        describe_service's own apis list, useful once you already know the service
        and just need the endpoint list. Capped at `limit` items (default 50) starting
        at `offset`; `total`/`truncated` in the response tell you whether to page
        further. Pass repository when the service name is duplicated. Next: describe_api for a specific one."""
        with closing(_conn()) as conn:
            return queries.list_apis(conn, service, limit, offset, repository)

    @mcp.tool()
    def describe_api(service: str, method: str, path: str, repository: str | None = None) -> dict:
        """The most detailed level for one API: response shape field by field, its
        calls to other services/queues (business reason + exact data needed), and
        validation/authorization rules. `api_shape` restates method/path/request/
        response compactly as one structured block (a lean "swagger"), including
        literal request/response header names and, when a
        SecurityFilterChain/@PreAuthorize/@Secured rule covers this route, its
        `security` requirement and roles (jvm-spring only so far for both) -- plus a
        literal `endpoint_kind` ("health_check"/"internal"/"rest", from the path
        alone). Call this once you know exactly which endpoint a change touches and
        need its full contract before editing it. Pass repository when needed."""
        with closing(_conn()) as conn:
            return queries.describe_api(conn, service, method, path, repository)

    @mcp.tool()
    def list_entrypoints(
        service: str, limit: int = queries.DEFAULT_LIST_LIMIT, offset: int = 0, repository: str | None = None,
    ) -> dict:
        """List HTTP, GraphQL, message, CLI and job entrypoints for one service.
        Call this after describe_service when an agent needs to select one narrow flow.
        Next: describe_entrypoint on the relevant item; responses are paginated. Pass repository when needed."""
        with closing(_conn()) as conn:
            return queries.list_entrypoints(conn, service, limit, offset, repository)

    @mcp.tool()
    def describe_entrypoint(
        service: str,
        kind: str,
        method: str,
        name: str,
        max_edges: int = queries.DEFAULT_FLOW_EDGE_LIMIT,
        repository: str | None = None,
    ) -> dict:
        """Return one entrypoint plus its deterministic local flow: invocations,
        validation, reads/writes and messages, each with evidence and provenance.
        `message_operations` reports reached publish/consume calls; a channel is
        confirmed only by a matching source contract, otherwise it is unknown.
        `external_http_calls` reports source-proven domains separately from
        indexed service calls; it does not infer an internal service identity.
        Includes a GraphQL argument/input/return contract when a local schema proves it.
        For an exact local HTTP route, includes a matching literal OpenAPI/Swagger
        operation when one is present in a conventional spec file.
        Protobuf declarations appear as gRPC entrypoints with a wire signature only;
        they do not imply that a handler or client has been resolved.
        `max_edges` defaults to 50 and is capped at 200 so a deep flow cannot flood
        agent context; `flow_pagination.truncated` tells the caller to ask again with
        a larger budget. Includes reachable literal resilience limits when source proves
        them; they are declarations, not runtime guarantees. `sequence_mermaid` is the
        same flow as a Mermaid sequenceDiagram (capped at 20 messages, independent of
        max_edges, for readability), zero LLM cost. This is the preferred narrow
        context primitive before reading source files."""
        with closing(_conn()) as conn:
            return queries.describe_entrypoint(conn, service, kind, method, name, max_edges, repository)

    @mcp.tool()
    def describe_error_flow(
        service: str, kind: str, method: str, name: str, repository: str | None = None,
    ) -> dict:
        """Trace one source-proven HTTP error across an indexed client boundary.
        It returns a flow only when the caller's reachable static client call, the
        target endpoint's reachable error contract, and the caller's matching local
        mapping are all explicit. Missing links remain unknown; values, messages,
        payloads and stack traces are never returned. Use describe_entrypoint first
        to select the narrow flow and pass repository for duplicate service names."""
        with closing(_conn()) as conn:
            return queries.describe_error_flow(conn, service, kind, method, name, repository)

    @mcp.tool()
    def ingest_runtime_evidence(
        service: str, source: str, observations: list[dict], repository: str | None = None,
    ) -> dict:
        """Ingest normalized OpenTelemetry or broker flow observations. Each item is
        exactly {from, to, kind, count}; trace IDs, attributes, payloads and source
        excerpts are rejected and never persisted. Runtime evidence stays separate
        from static analysis; use describe_runtime_divergence to compare them."""
        with closing(_conn()) as conn:
            return queries.ingest_runtime_evidence(conn, service, source, observations, repository)

    @mcp.tool()
    def describe_runtime_divergence(service: str, repository: str | None = None) -> dict:
        """Compare normalized runtime observations with static flow facts. An
        unobserved static edge is explicitly not treated as dead code because traces
        may be sampled or lack coverage."""
        with closing(_conn()) as conn:
            return queries.describe_runtime_divergence(conn, service, repository)

    @mcp.tool()
    def list_security_findings(service: str, repository: str | None = None) -> dict:
        """List deterministic security findings for one service. Values of secrets and
        source excerpts are never returned. Use this before planning a change that
        touches configuration, credentials or an external integration. Pass repository when needed."""
        with closing(_conn()) as conn:
            return queries.list_security_findings(conn, service, repository)

    @mcp.tool()
    def describe_persistence(
        service: str, limit: int = queries.DEFAULT_LIST_LIMIT, offset: int = 0, repository: str | None = None,
    ) -> dict:
        """Full field-level schema of everything one microservice persists (tables/
        documents/caches), plus source-proven SQL or Liquibase XML migration operations, including the concrete engine (postgres/mysql/mongodb/
        cassandra/dynamodb/redis/elasticsearch/sqlite/unknown) — describe_service only names
        these, this returns the actual fields. Migration facts are independently
        paginated and state only literal source operations, never execution state.
        Capped at `limit` entities (default 50) starting at `offset`;
        `total`/`truncated` tell you whether to page further.
        Call this before changing anything that reads or writes this service's
        storage. Pass repository when the service name is duplicated."""
        with closing(_conn()) as conn:
            return queries.describe_persistence(conn, service, limit, offset, repository)

    @mcp.tool()
    def describe_configuration(
        service: str, limit: int = queries.DEFAULT_LIST_LIMIT, offset: int = 0, repository: str | None = None,
    ) -> dict:
        """List literal environment and JVM property keys read by local symbols.
        Exact Spring ``@Value`` and ``@ConfigurationProperties`` bindings on supported
        members are included. Exact Kubernetes ``env`` references for a returned
        environment key are included compactly; values, dynamic keys, SpEL and runtime resolution are excluded. Capped at `limit`
        bindings (default 50) starting at `offset`; `total`/`truncated` tell you
        whether to page further. Pass repository when the service name is duplicated."""
        with closing(_conn()) as conn:
            return queries.describe_configuration(conn, service, limit, offset, repository)

    @mcp.tool()
    def describe_runtime_configuration(
        service: str, limit: int = queries.DEFAULT_LIST_LIMIT, offset: int = 0, repository: str | None = None,
        workloads: list[dict] | None = None, binding_workloads: list[dict] | None = None,
        source_import_workloads: list[dict] | None = None,
        source_import_declaration_statuses: list[str] | None = None,
        source_import_availabilities: list[str] | None = None,
        binding_declaration_statuses: list[str] | None = None,
        binding_source_kinds: list[str] | None = None,
        source_import_source_kinds: list[str] | None = None,
        source_import_container_roles: list[str] | None = None,
        source_import_prefixes: list[str] | None = None,
        source_import_include_unprefixed: bool = False,
        binding_evidence_files: list[str] | None = None,
        source_import_evidence_files: list[str] | None = None,
        binding_evidence_ranges: list[dict] | None = None,
        source_import_evidence_ranges: list[dict] | None = None,
    ) -> dict:
        """List literal Kubernetes workload environment references to ConfigMaps
        and Secrets, plus literal ``envFrom`` sources with unknown per-key coverage.
        The workload identifies application versus initialization containers when
        known. Literal source availability is reported when known; a locally
        unresolved source is an ownership hypothesis, not a missing-source finding.
        Values, dynamic names and unrendered Helm templates are excluded.
        Both lists are capped at `limit` (default 50) starting at `offset`; up to 500
        workload identities optionally limits both or either list to exact Kubernetes
        scopes. Source imports and bindings can also be filtered by static
        declaration-finding state and literal source kind; imports support literal
        availability, indexed container role, exact envFrom prefix and exact evidence
        file or overlapping line range. Use at most four filter dimensions per list.
        Active filters return compact selected-versus-indexed totals. Pass repository
        when service names duplicate."""
        with closing(_conn()) as conn:
            return queries.describe_runtime_configuration(
                conn, service, limit, offset, repository, workloads, binding_workloads, source_import_workloads,
                source_import_declaration_statuses, source_import_availabilities, binding_declaration_statuses,
                binding_source_kinds, source_import_source_kinds, source_import_container_roles,
                source_import_prefixes, source_import_include_unprefixed, binding_evidence_files,
                source_import_evidence_files, binding_evidence_ranges, source_import_evidence_ranges,
            )

    @mcp.tool()
    def validate_runtime_configuration_follow_up(
        plan_id: str,
        change_unit_id: str,
        returned_workloads: list[dict],
        source_import_truncated: bool,
        current_offset: int = 0,
        current_limit: int = queries.DEFAULT_LIST_LIMIT,
        previous_page_fingerprint: str | None = None,
        include_matched_workloads: bool = False,
        include_trace_details: bool = False,
    ) -> dict:
        """Validate whether one runtime-configuration page covered a truncated plan target.
        Pass direct workload identities or the query's source_imports entries. It returns
        whether another page is useful; it reads only persisted plan facts and never calls
        a model or scans source. Set include_matched_workloads or include_trace_details
        for detailed audit output."""
        with closing(_conn()) as conn:
            return queries.validate_runtime_configuration_follow_up(
                conn, plan_id, change_unit_id, returned_workloads, source_import_truncated, current_offset, current_limit,
                previous_page_fingerprint, include_matched_workloads, include_trace_details,
            )

    @mcp.tool()
    def describe_feature_flags(
        service: str, limit: int = queries.DEFAULT_LIST_LIMIT, offset: int = 0, repository: str | None = None,
    ) -> dict:
        """List literal feature-flag reads through a locally proven SDK. It returns
        only key, provider and source evidence: values, targeting rules, rollout state,
        dynamic keys and runtime evaluation are excluded. Capped at `limit` bindings
        (default 50) starting at `offset`; pass repository when service names duplicate."""
        with closing(_conn()) as conn:
            return queries.describe_feature_flags(conn, service, limit, offset, repository)

    @mcp.tool()
    def describe_messages(
        service: str, limit: int = queries.DEFAULT_LIST_LIMIT, offset: int = 0, repository: str | None = None,
    ) -> dict:
        """Full field-level shape of async messages one microservice publishes/
        consumes, including the concrete broker (kafka/rabbitmq/sqs/sns/service_bus/
        activemq/nats/unknown) — describe_service only names the channels, this
        returns the actual payload shape. Capped at `limit` messages (default 50)
        starting at `offset`; `total`/`truncated` tell you whether to page further.
        Call this before changing an event's contract; then check
        get_relationships/find_change_surface's contracts_at_risk for who else
        consumes it. Pass repository when the service name is duplicated."""
        with closing(_conn()) as conn:
            return queries.describe_messages(conn, service, limit, offset, repository)

    @mcp.tool()
    def describe_cloud_dependencies(
        service: str, limit: int = queries.DEFAULT_LIST_LIMIT, offset: int = 0, repository: str | None = None,
    ) -> dict:
        """What one microservice deterministically talks to in the cloud (AWS
        SQS/SNS/S3/EventBridge, Azure Blob Storage): `static_facts` is every proven
        SDK call site in its code (provider, service, operation, evidence — never a
        keyword guess, only a locally-declared client/import actually constructed or
        called), and `iac_resources` is what Terraform/CloudFormation/Kubernetes in
        this repository declares and structurally attributes to this service. The
        two are reported separately and never merged into one claim: code without a
        matching declaration, or a declaration nothing in code references, are both
        left for you to interpret, not resolved here — see find_architecture_smells
        for a system-wide check of that gap. Capped at `limit` items per list
        (default 50) starting at `offset`; each list's own `total`/`truncated` in
        `pagination` tells you whether to page further. Call this before changing
        anything that publishes, consumes, reads or writes a cloud resource. Pass
        repository when the service name is duplicated."""
        with closing(_conn()) as conn:
            return queries.describe_cloud_dependencies(conn, service, limit, offset, repository)

    @mcp.tool()
    def search(query: str, repository: str | None = None) -> dict:
        """Keyword search across services, APIs, persistence entities and message
        relationships (SQLite FTS5, not semantic). Optionally pass repository to
        scope a cumulative KB. Call this when you don't know
        which service/API a keyword belongs to. Next: describe_service or
        describe_api on whatever it turns up."""
        with closing(_conn()) as conn:
            return queries.search(conn, query, repository)

    @mcp.tool()
    def get_relationships(service: str, direction: str = "both", repository: str | None = None) -> dict:
        """Graph of edges around one service: outbound calls it makes, inbound calls other
        services make into it ('who depends on me'), and queue/topic links inferred from
        matching publish/consume channel names. direction: 'outbound', 'inbound' or 'both'.
        Each edge carries its business reason, confidence, provenance (llm vs.
        deterministic) and evidence (file/line) when known — this is the navigation
        primitive behind find_change_surface. Call this once you know a service and
        want its immediate neighborhood. Pass repository when the service name is
        duplicated. Next: trace_flow if the service you're
        looking for isn't a direct neighbor, or describe_api on a specific edge's API."""
        with closing(_conn()) as conn:
            return queries.get_relationships(conn, service, direction, repository)

    @mcp.tool()
    def trace_flow(
        from_service: str,
        to_service: str,
        max_hops: int = 6,
        from_repository: str | None = None,
        to_repository: str | None = None,
    ) -> dict:
        """Shortest path connecting two services, walking outbound calls and
        publish->consume message links (the multi-hop counterpart to
        get_relationships' single hop). Use this when you know two services are
        related but not how — e.g. 'does checkout-service's request ever reach
        ledger-service, and through what?'. Each hop carries its business reason and
        evidence when known. Pass from_repository and to_repository independently
        when either endpoint name is duplicated. Next: describe_api/describe_messages
        on the hop that looks most relevant to your change."""
        with closing(_conn()) as conn:
            return queries.trace_flow(conn, from_service, to_service, max_hops, from_repository, to_repository)

    @mcp.tool()
    def find_architecture_smells() -> dict:
        """Deterministic, whole-graph structural findings computed purely from already-
        indexed facts (service_calls, persistence_entities, direct static flow_edges
        and persistence ownership declarations)
        — no LLM call, recomputed
        after every index/update. Reports: cycle (a circular dependency among internal
        services — A depends on B depends on ... depends on A), fan_in/fan_out (a
        service with an unusually high number of direct internal dependents/
        dependencies — a bottleneck or orchestrator candidate), shared_database (two or
        more services persisting a same-named entity on the same engine — likely
        sharing a database, coupling their schemas), and duplicate_external_integration
        (two or more services independently integrating with the same third-party
        vendor). It also reports bounded flow hypotheses: possible_bff_domain_leakage
        for a GraphQL mutation that directly writes or publishes, and
        possible_non_atomic_publish when one entrypoint writes and publishes without
        a source-proven transaction boundary, plus possible_read_entrypoint_side_effect
        when a static write or publication is directly reachable from an HTTP safe
        method or GraphQL query. possible_aggregate_ownership_overlap reports distinct
        services declaring ownership of the same static table/document; its detail
        preserves each declared owner without assuming a shared database.
        possible_message_consumer_without_recovery_policy is a low-confidence signal
        for a RabbitMQ consumer whose indexed contract proves no retry boundary,
        retry delay or dead-letter route; it does not claim that external broker
        configuration is missing. Each finding carries a plain-language reason,
        services, confidence, source evidence, explicit unknowns and conservative
        remediation, in risk
        language ('likely', 'worth checking') — never a confirmed
        verdict; this describes structure, not a judgment call only a human/LLM
        synthesis over real evidence could make. When a prior run exists, the response
        also includes trend: new_findings (appeared since the last index/update),
        resolved_findings (gone since then) and count_deltas (a fan_in/fan_out count
        that changed while the finding itself persisted) — computed for free from
        already-stored runs, no re-detection. trend is omitted entirely (not an empty
        object) on the very first run ever, since there's nothing yet to compare
        against. Call this for a system-wide health check without reading any source
        file. Next: get_relationships/trace_flow on a flagged service to see the edges
        behind a finding, or describe_service to understand why it's shaped that way."""
        with closing(_conn()) as conn:
            return queries.find_architecture_smells(conn)

    @mcp.tool()
    def find_change_surface(
        task: str, hint_services: list[str] | None = None, repository: str | None = None,
    ) -> dict:
        """Given a business task/epic description, find which indexed services likely
        need code changes — WITHOUT reading any source file. Call this FIRST when handed
        an epic, before exploring the codebase. Returns primary/secondary/no_change_hint
        service lists plus a relevant call flow, each finding with a business reason,
        confidence (0-1) and evidence (file/line); also external_integrations (third-party
        vendors/SaaS reachable from the relevant services — you may need to touch that
        integration too), unmapped_internal_hint (dependencies that look like internal
        services of this same system but haven't been indexed yet — index them for a
        fuller picture), contracts_at_risk (events a relevant service publishes and
        every other indexed service that consumes them — potentially affected, never
        declared a confirmed break) and persistence_affected (what storage each
        relevant service owns, read straight from the index, no extra call needed for
        the obvious cases). unknowns lists every gap in the answer explicitly (status,
        reason, suggestion) instead of silently omitting it — including a relevant
        service whose freshness came back stale, since its stored dependency reasons
        may themselves be out of date. freshness reports, per relevant service,
        whether its indexed knowledge might be stale (its indexed commit vs. the
        repository's current commit). recommended_next_queries is a
        ranked list of {tool, arguments, reason} — the highest-value MCP tool calls to
        make next given what's already known, computed for free with no extra LLM
        cost; prefer it over exploring blindly. This is a task-specific inference,
        not a verified fact — treat it as a starting point, not ground truth. Pass
        hint_services if you already suspect specific services, to anchor the search.
        If the KB contains duplicate service names, repository is required and scopes
        retrieval, the generated context and recommended next calls to that repository.
        The response includes a run_id — pass it to record_change_surface_feedback once
        you know whether the findings were actually right, to improve future confidence
        for this service. run_cost_usd reports this call's own LLM cost when the
        backend's CLI exposed it, and null (never fabricated as 0) when it didn't.
        similar_past_tasks lists up to 3 earlier find_change_surface runs whose task
        text was semantically closest to this one (only populated when the optional
        `semantic` extra is installed — empty otherwise, never an error), each with
        its own primary_services and an honest outcome ('verified precision=...
        recall=...' from a real git check, 'feedback: N confirmed, M rejected' from
        self-reported outcomes, or 'no feedback yet') — historical precedent for
        whether a similarly-worded task actually panned out."""
        with track_local_activity(db_path, "find_change_surface"):
            with closing(_conn()) as conn:
                return queries.find_change_surface(conn, resolved_backend, task, hint_services, repository)

    @mcp.tool()
    def get_change_context(
        task: str,
        hint_services: list[str] | None = None,
        repository: str | None = None,
        max_services: int = 3,
        epic_type: str = "unspecified",
    ) -> dict:
        """Compact first briefing for an engineering epic. It runs the same bounded
        change-surface inference as find_change_surface, then adds at most
        max_services (1-5, default 3) compact service cards: interfaces, outbound
        dependencies, persistence and messages. It also carries already-indexed
        architecture risks, contracts at risk, explicit unknowns and next queries.
        No source file is read and no lower-level tool is replaced: use the returned
        recommended_next_queries or describe_entrypoint/describe_api for detail.
        Pass repository when service names are duplicated; it scopes both inference
        and every compact card. Call this when an agent needs enough context to draft
        a plan in one response without filling its context window with full services."""
        with track_local_activity(db_path, "get_change_context"):
            with closing(_conn()) as conn:
                return queries.get_change_context(
                    conn, resolved_backend, task, hint_services, repository, max_services, epic_type,
                )

    @mcp.tool()
    def plan_change(
        task: str,
        hint_services: list[str] | None = None,
        repository: str | None = None,
        token_budget: int = queries.DEFAULT_PLAN_TOKEN_BUDGET,
    ) -> dict:
        """Start an evidence-first, bounded change plan for a free-text task.

        The initial response identifies the indexed surface and explicit unknowns;
        it only adds decision points and change units whose dependencies can be proved
        from the knowledge base. Pass repository when service names are duplicated.
        token_budget is capped at 2200 measured response tokens. The response labels
        whether its count came from the optional local tokenizer or byte estimate.
        """
        with track_local_activity(db_path, "plan_change"):
            with closing(_conn()) as conn:
                return queries.plan_change(conn, resolved_backend, task, hint_services, repository, token_budget)

    @mcp.tool()
    def refine_change_plan(plan_id: str, decisions: list[dict]) -> dict:
        """Resolve every pending plan decision without rerunning retrieval or an LLM.

        decisions must select exactly one declared option for each pending decision.
        Finalized decisions are immutable: retrying the same selection is safe, while
        changing one requires a new plan so its impact can be evaluated explicitly.
        """
        with closing(_conn()) as conn:
            return queries.refine_change_plan(conn, plan_id, decisions)

    @mcp.tool()
    def describe_change_unit(plan_id: str, change_unit_id: str) -> dict:
        """Return one accepted change unit and only its required follow-up context.

        The result is sourced from the persisted plan. It recommends the narrow
        message, endpoint, or persistence query appropriate to that unit; it never
        scans files or reruns retrieval.
        """
        with closing(_conn()) as conn:
            return queries.describe_change_unit(conn, plan_id, change_unit_id)

    @mcp.tool()
    def assess_working_change(plan_id: str, repository: str, since_commit: str) -> dict:
        """Compare a ready change plan with one repository's Git diff.

        This read-only, deterministic advisory marks a unit covered only when a
        source-evidence file changed. It reports unassessable units rather than
        guessing, and never calls an LLM or blocks an implementation. When the diff
        reaches a planned service, it also returns safe indexed test/build hints
        without executing them.
        """
        with track_local_activity(db_path, "assess_working_change"):
            with closing(_conn()) as conn:
                return queries.assess_working_change(conn, plan_id, repository, since_commit)

    @mcp.tool()
    def record_ci_validation_result(
        plan_id: str,
        repository: str,
        workflow_path: str,
        start_line: int,
        status: str,
        duration_ms: int | None = None,
    ) -> dict:
        """Record an agent-reported result for one indexed CI test/build command.

        This never runs a command and accepts only the exact workflow path and line
        of an indexed safe test/build command. It stores the latest passed/failed
        state and optional duration, never stdout, stderr, notes or source content.
        Call assess_working_change afterwards to see the compact result in context.
        """
        with closing(_conn()) as conn:
            return queries.record_ci_validation_result(
                conn, plan_id, repository, workflow_path, start_line, status, duration_ms,
            )

    @mcp.tool()
    def record_change_unit_validation_result(
        plan_id: str, change_unit_id: str, check_index: int, status: str,
    ) -> dict:
        """Record passed/failed for one persisted manual check on a ready plan unit.

        check_index is the zero-based position in describe_change_unit.validation.
        This tool accepts neither check text nor notes, so it can only report a
        preplanned validation obligation and never stores source or free-form data.
        """
        with closing(_conn()) as conn:
            return queries.record_change_unit_validation_result(
                conn, plan_id, change_unit_id, check_index, status,
            )

    @mcp.tool()
    def describe_change_plan_validation_status(
        plan_id: str,
        limit: int = queries.DEFAULT_PLAN_VALIDATION_UNIT_LIMIT,
        offset: int = 0,
    ) -> dict:
        """List bounded manual-validation state for a ready plan without check text.

        Use this to find pending or failed units without requesting every unit's
        checklist. It returns only unit IDs, counts and statuses; describe_change_unit
        remains the narrow follow-up for a specific check and its wording.
        """
        with closing(_conn()) as conn:
            return queries.describe_change_plan_validation_status(conn, plan_id, limit, offset)

    @mcp.tool()
    def describe_change_validation_status(plan_id: str, repository: str) -> dict:
        """Summarize current agent-reported CI validation for one ready change plan.

        Status is advisory: reported_passed means each compact indexed test/build
        command has a passed report, not that the requested change is approved or
        all manual checks are complete. The tool never executes commands or returns
        process output.
        """
        with closing(_conn()) as conn:
            return queries.describe_change_validation_status(conn, plan_id, repository)

    @mcp.tool()
    def review_change_closure(plan_id: str, repository: str, since_commit: str) -> dict:
        """Return an advisory closing summary for a ready plan and one Git diff.

        It combines unit coverage, public-error-contract risk and agent-reported
        indexed CI validation. ready_for_manual_review is not deployment approval:
        manual checks and unproven runtime behavior remain outside this bounded view.
        """
        with closing(_conn()) as conn:
            return queries.review_change_closure(conn, plan_id, repository, since_commit)

    @mcp.tool()
    def record_change_context_feedback(
        run_id: int,
        outcome: str,
        note: str | None = None,
        missing_services: list[str] | None = None,
    ) -> dict:
        """Record whether a get_change_context briefing was sufficient, insufficient
        or excessive. run_id comes from its telemetry field. A supplied note is not
        stored as text: OrbitKB retains only a one-way digest and service IDs, so do
        not put source code or prompt content in it. missing_services is useful when
        outcome is insufficient. This feedback calibrates budget policy; it never
        changes the current 1-5 safety cap by itself."""
        with closing(_conn()) as conn:
            return queries.record_change_context_feedback(conn, run_id, outcome, note, missing_services)

    @mcp.tool()
    def record_context_query_execution(run_id: int, tool: str, service: str | None = None) -> dict:
        """Record one recommended follow-up query after it was executed. This links
        a get_change_context telemetry run to its progressive-disclosure path. Only
        a recommended tool and optional service ID are accepted; no arguments,
        prompt text or response content is retained."""
        with closing(_conn()) as conn:
            return queries.record_context_query_execution(conn, run_id, tool, service)

    @mcp.tool()
    def get_context_budget_metrics(epic_type: str | None = None) -> dict:
        """Return privacy-safe context calibration aggregates: budget distribution,
        truncation, sufficient/insufficient/excessive feedback, mean response size,
        query follow-through and the current history-based recommendation. No task,
        source, prompt or compact-card content is returned."""
        with closing(_conn()) as conn:
            return queries.get_context_budget_metrics(conn, epic_type)

    @mcp.tool()
    def verify_context_budget(run_id: int, repository: str, since_commit: str) -> dict:
        """When Git history is available, compare delivered context cards with the
        services actually changed since a commit. Returns precision, recall and
        omission_rate, and retains service IDs rather than source or task content."""
        with closing(_conn()) as conn:
            return queries.verify_context_budget(conn, run_id, repository, since_commit)

    @mcp.tool()
    def record_change_surface_feedback(run_id: int, service: str, outcome: str) -> dict:
        """Report whether a find_change_surface finding was actually right: call this
        AFTER you've acted on a change surface result, once you know whether a given
        service really needed a change. outcome: 'confirmed' (it did) or 'rejected'
        (it didn't). run_id comes from a prior find_change_surface response. This
        closes the feedback loop — future find_change_surface confidence for this
        service is nudged by its track record. See also verify_change_surface,
        which computes this automatically from a real git diff instead of you
        having to know the outcome by hand."""
        with closing(_conn()) as conn:
            return queries.record_change_surface_feedback(conn, run_id, service, outcome)

    @mcp.tool()
    def verify_change_surface(run_id: int, repository: str, since_commit: str) -> dict:
        """Ground-truth check: compare a past find_change_surface run's predicted
        services against what the given repository's commits actually changed
        (git diff) since since_commit. Read-only — it does not itself record
        feedback; call record_change_surface_feedback for that. Returns predicted,
        actual, true_positives, false_positives, false_negatives, precision and
        recall. repository is the name shown by list_services'/index's
        --repository-name, not a service name."""
        with closing(_conn()) as conn:
            return queries.verify_change_surface(conn, run_id, repository, since_commit)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="orbitkb serve")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--backend", choices=["claude", "codex"], default=None, help="Used by find_change_surface and get_change_context")
    parser.add_argument("--model", default=None)
    parser.add_argument("--claude-bare", action="store_true")
    parser.add_argument("--codex-api-key", action="store_true")
    args = parser.parse_args()
    backend = resolve_backend(args.backend, args.model, args.claude_bare, args.codex_api_key)
    server = build_server(args.db, backend=backend)
    server.run()


if __name__ == "__main__":
    main()
