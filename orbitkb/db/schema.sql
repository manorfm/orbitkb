-- OrbitKB SQLite schema. Single source of DDL truth.
-- No backward compatibility is maintained across versions: this file is the only
-- shape a database is expected to have. Breaking changes replace old columns/tables
-- outright instead of growing compatibility shims.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repositories (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    root_path     TEXT NOT NULL UNIQUE,
    vcs_url       TEXT,
    default_branch TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    root_path     TEXT NOT NULL,
    repository_id INTEGER REFERENCES repositories(id) ON DELETE SET NULL,
    stack         TEXT,
    short_desc    TEXT,
    long_desc     TEXT,
    updated_at    TEXT NOT NULL,
    last_commit   TEXT,
    UNIQUE(repository_id, name)
);

-- Literal validation commands declared by a repository GitHub Actions workflow.
-- Dynamic, multiline and secret-bearing commands are filtered before persistence.
CREATE TABLE IF NOT EXISTS ci_commands (
    id            INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    workflow_path TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('test', 'build', 'migration', 'client_generation')),
    command       TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    start_line    INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(repository_id, workflow_path, kind, command, start_line)
);
CREATE INDEX IF NOT EXISTS idx_ci_commands_repository ON ci_commands(repository_id);

CREATE TABLE IF NOT EXISTS apis (
    id             INTEGER PRIMARY KEY,
    service_id     INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    method         TEXT NOT NULL,
    path           TEXT NOT NULL,
    summary        TEXT,
    description    TEXT,
    response_shape TEXT,
    request_shape  TEXT,
    evidence_json  TEXT,
    updated_at     TEXT NOT NULL,
    UNIQUE(service_id, method, path)
);

CREATE TABLE IF NOT EXISTS api_validations (
    id          INTEGER PRIMARY KEY,
    api_id      INTEGER NOT NULL REFERENCES apis(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('input_validation', 'authorization')),
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_calls (
    id              INTEGER PRIMARY KEY,
    from_service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    from_api_id     INTEGER REFERENCES apis(id) ON DELETE CASCADE,
    to_service_name TEXT NOT NULL,
    to_service_id   INTEGER REFERENCES services(id) ON DELETE SET NULL,
    call_kind       TEXT NOT NULL CHECK (call_kind IN ('http', 'grpc', 'queue_publish', 'queue_consume')),
    reason          TEXT,
    data_needed     TEXT,
    purpose_kind    TEXT CHECK (purpose_kind IN ('validation', 'data_fetch', 'enrichment', 'notification', 'other')),
    confidence      REAL,
    target_kind     TEXT CHECK (target_kind IN ('internal', 'external', 'unknown')) DEFAULT 'unknown',
    -- Only meaningful when target_kind = 'external': what kind of resource it is.
    -- Same precedence as target_kind — LLM judgment on real code first, the
    -- deterministic vendor-keyword list (integration_heuristics.classify_resource_type)
    -- only fills gaps left 'unknown'.
    resource_type   TEXT CHECK (resource_type IN ('queue', 'storage', 'compute', 'saas', 'db_managed', 'other', 'not_applicable')) DEFAULT 'not_applicable',
    evidence_json   TEXT,
    updated_at      TEXT NOT NULL
);

-- `engine` is the concrete database (postgres, mysql, mongodb, cassandra, dynamodb,
-- redis, elasticsearch, sqlite), inferred by the LLM the same way messages.provider is —
-- an ORM model/entity definition (SQLAlchemy, JPA, GORM) rarely names its own engine,
-- so this leans on the service's dependency manifest and config files as evidence.
-- 'unknown' means neither resolved it; never a guess.
CREATE TABLE IF NOT EXISTS persistence_entities (
    id            INTEGER PRIMARY KEY,
    service_id    INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    kind          TEXT CHECK (kind IN ('sql_table', 'document', 'cache', 'other')),
    engine        TEXT NOT NULL DEFAULT 'unknown',
    schema_json   TEXT,
    evidence_json TEXT,
    updated_at    TEXT NOT NULL,
    UNIQUE(service_id, name)
);

CREATE TABLE IF NOT EXISTS static_persistence_facts (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('sql_table', 'document')),
    owner       TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Literal operations from conventional SQL migration directories. These are source
-- facts, not an assertion that the migration ran in any environment.
CREATE TABLE IF NOT EXISTS static_migration_facts (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    operation   TEXT NOT NULL CHECK (operation IN ('create_table', 'add_column', 'drop_column', 'drop_table', 'create_index')),
    table_name  TEXT NOT NULL,
    column_name TEXT,
    destructive INTEGER NOT NULL CHECK (destructive IN (0, 1)),
    file_path   TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Literal configuration keys read by a local source symbol. Values, `.env` files
-- and runtime resolution are deliberately excluded from this static fact table.
CREATE TABLE IF NOT EXISTS static_configuration_bindings (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    key         TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('environment', 'property')),
    sensitive   INTEGER NOT NULL CHECK (sensitive IN (0, 1)),
    file_path   TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_static_configuration_bindings_service
    ON static_configuration_bindings(service_id);

-- Literal feature-flag reads through a locally proven SDK. Flag values and
-- rollout state are runtime concerns and deliberately never enter the KB.
CREATE TABLE IF NOT EXISTS static_feature_flags (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    key         TEXT NOT NULL,
    provider    TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_static_feature_flags_service
    ON static_feature_flags(service_id);

-- One authorization rule, proven either for a route pattern (a SecurityFilterChain's
-- authorizeHttpRequests block: route_pattern/method set, symbol NULL) or for a specific
-- symbol (@PreAuthorize/@Secured: symbol set, route_pattern/method NULL). roles_json is
-- a best-effort, possibly empty list resolved from the requirement's own code -- never a
-- proof of how a custom AuthorizationManager's roles actually combine at runtime.
CREATE TABLE IF NOT EXISTS static_security_requirements (
    id            INTEGER PRIMARY KEY,
    service_id    INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    route_pattern TEXT,
    method        TEXT,
    symbol        TEXT,
    requirement   TEXT NOT NULL,
    roles_json    TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    start_line    INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_static_security_requirements_service
    ON static_security_requirements(service_id);

-- One HTTP header name a specific route reads (direction='request', from
-- @RequestHeader) or writes (direction='response', from a ResponseEntity header
-- builder call) -- the name only, never a value (see ApiHeader in models.py).
CREATE TABLE IF NOT EXISTS static_api_headers (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('request', 'response')),
    name        TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_static_api_headers_service_route
    ON static_api_headers(service_id, method, path);

-- `provider` is the concrete message broker/vendor (kafka, rabbitmq, sqs, sns,
-- service_bus, activemq, nats), inferred by the LLM from real code the same way
-- service_calls.target_kind is — 'unknown' means the code only showed a
-- transport-agnostic abstraction (JMS, Celery, NestJS microservices) and no
-- configuration evidence resolved it; never a guess.
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,
    service_id    INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    direction     TEXT NOT NULL CHECK (direction IN ('publishes', 'consumes')),
    channel       TEXT NOT NULL,
    shape_json    TEXT,
    description   TEXT,
    provider      TEXT NOT NULL DEFAULT 'unknown',
    evidence_json TEXT,
    updated_at    TEXT NOT NULL,
    UNIQUE(service_id, direction, channel)
);

CREATE TABLE IF NOT EXISTS static_message_contracts (
    id            INTEGER PRIMARY KEY,
    service_id    INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    direction     TEXT NOT NULL CHECK (direction IN ('publishes', 'consumes')),
    channel       TEXT NOT NULL,
    routing_key   TEXT,
    payload_type  TEXT,
    message_version TEXT,
    file_path     TEXT NOT NULL,
    start_line    INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Deterministic, AST-derived cloud SDK operations (AWS SQS/SNS/S3/EventBridge,
-- Azure Blob Storage), proven the same way GORM/JPA/Mongoose calls already are:
-- a locally-declared client type or a named SDK import, matched against the
-- vendor-sourced tables in orbitkb/analysis/cloud_taxonomy.py — never a keyword
-- guess. Rewritten wholesale by flows_repo.replace_analysis on every scan, never
-- hand-edited. `target_name` is the literal queue/bucket/topic name only when the
-- call site names it; NULL means unresolved, never a guess.
CREATE TABLE IF NOT EXISTS static_cloud_facts (
    id             INTEGER PRIMARY KEY,
    service_id     INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    provider       TEXT NOT NULL CHECK (provider IN ('aws', 'azure', 'gcp')),
    resource_type  TEXT NOT NULL CHECK (resource_type IN ('queue', 'pubsub', 'event_bus', 'object_storage', 'stream')),
    service_name   TEXT NOT NULL,
    operation      TEXT NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('publish', 'consume', 'read', 'write', 'admin')),
    sdk            TEXT NOT NULL,
    target_name    TEXT,
    file_path      TEXT NOT NULL,
    start_line     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_static_cloud_facts_service ON static_cloud_facts(service_id);

-- Structurally-parsed IaC declarations (Terraform via python-hcl2, CloudFormation
-- via cfn-flip, plain Kubernetes manifests via PyYAML — real parsers, never
-- regex/keyword heuristics). Repository-scoped, not service-scoped: IaC commonly
-- lives outside any single service's own root (infra/, terraform/, deploy/).
-- service_id is only ever set when the declaring file provably falls under
-- exactly one indexed service's root; NULL means "belongs to this repository,
-- ownership not resolvable", never a guess. `physical_name` is NULL whenever the
-- source attribute is an interpolated expression rather than a literal.
CREATE TABLE IF NOT EXISTS cloud_iac_resources (
    id                  INTEGER PRIMARY KEY,
    repository_id       INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    service_id          INTEGER REFERENCES services(id) ON DELETE SET NULL,
    provider            TEXT NOT NULL CHECK (provider IN ('aws', 'azure', 'gcp')),
    resource_type       TEXT NOT NULL CHECK (resource_type IN ('queue', 'pubsub', 'event_bus', 'object_storage', 'stream')),
    iac_resource_type   TEXT NOT NULL,
    logical_name        TEXT NOT NULL,
    physical_name       TEXT,
    source_format       TEXT NOT NULL CHECK (source_format IN ('terraform', 'cloudformation', 'kubernetes', 'compose_hint')),
    confidence          TEXT NOT NULL CHECK (confidence IN ('high', 'low')) DEFAULT 'high',
    file_path           TEXT NOT NULL,
    start_line          INTEGER NOT NULL,
    end_line            INTEGER NOT NULL,
    updated_at          TEXT NOT NULL,
    -- JSON object of a small, curated set of literal attributes worth tracking
    -- per resource type (see cloud_taxonomy.IAC_PRESENCE_ATTRIBUTES/
    -- IAC_VALUE_ATTRIBUTES) — bool for presence-only attributes, string for
    -- value-matters ones. One flexible column instead of a new one per smell.
    attributes_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_cloud_iac_resources_repository ON cloud_iac_resources(repository_id);
CREATE INDEX IF NOT EXISTS idx_cloud_iac_resources_service ON cloud_iac_resources(service_id);

-- Literal Kubernetes Pod-template environment references. Values are never
-- indexed: a row only states that a workload variable comes from a ConfigMap
-- or Secret key declared in the manifest.
CREATE TABLE IF NOT EXISTS kubernetes_configuration_bindings (
    id              INTEGER PRIMARY KEY,
    repository_id   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    service_id      INTEGER REFERENCES services(id) ON DELETE SET NULL,
    environment_key TEXT NOT NULL,
    source_kind     TEXT NOT NULL CHECK (source_kind IN ('config_map', 'secret')),
    source_name     TEXT NOT NULL,
    source_key      TEXT NOT NULL,
    workload_kind   TEXT NOT NULL,
    workload_name   TEXT NOT NULL,
    container_name  TEXT NOT NULL,
    container_role  TEXT CHECK (container_role IN ('application', 'initialization')),
    file_path       TEXT NOT NULL,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kubernetes_configuration_bindings_repository
    ON kubernetes_configuration_bindings(repository_id);
CREATE INDEX IF NOT EXISTS idx_kubernetes_configuration_bindings_service
    ON kubernetes_configuration_bindings(service_id);

-- Literal ``envFrom`` source imports. Kubernetes resolves their keys at runtime,
-- so this stores source/workload metadata only and never creates per-key facts.
CREATE TABLE IF NOT EXISTS kubernetes_configuration_source_imports (
    id              INTEGER PRIMARY KEY,
    repository_id   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    service_id      INTEGER REFERENCES services(id) ON DELETE SET NULL,
    source_kind     TEXT NOT NULL CHECK (source_kind IN ('config_map', 'secret')),
    source_name     TEXT NOT NULL,
    prefix          TEXT,
    optional        INTEGER CHECK (optional IN (0, 1)),
    workload_kind   TEXT NOT NULL,
    workload_name   TEXT NOT NULL,
    container_name  TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kubernetes_configuration_source_imports_repository
    ON kubernetes_configuration_source_imports(repository_id);
CREATE INDEX IF NOT EXISTS idx_kubernetes_configuration_source_imports_service
    ON kubernetes_configuration_source_imports(service_id);

-- An ``envFrom`` source with no matching local ConfigMap/Secret declaration.
-- This is an ownership hypothesis: a chart or another delivery boundary may own it.
CREATE TABLE IF NOT EXISTS kubernetes_configuration_source_import_unknowns (
    id                   INTEGER PRIMARY KEY,
    repository_id        INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    service_id           INTEGER REFERENCES services(id) ON DELETE SET NULL,
    source_kind          TEXT NOT NULL CHECK (source_kind IN ('config_map', 'secret')),
    source_name          TEXT NOT NULL,
    prefix               TEXT,
    optional             INTEGER CHECK (optional IN (0, 1)),
    container_role       TEXT CHECK (container_role IN ('application', 'initialization')),
    workload_kind        TEXT,
    workload_name        TEXT,
    container_name       TEXT,
    reference_file_path  TEXT NOT NULL,
    reference_start_line INTEGER NOT NULL,
    reference_end_line   INTEGER NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kubernetes_configuration_source_import_unknowns_repository
    ON kubernetes_configuration_source_import_unknowns(repository_id);
CREATE INDEX IF NOT EXISTS idx_kubernetes_configuration_source_import_unknowns_service
    ON kubernetes_configuration_source_import_unknowns(service_id);

-- A source-proven local mismatch: a workload references a key that is absent
-- from its single ConfigMap/Secret declaration in this repository. This says
-- nothing about external resources, rendered templates, or runtime values.
CREATE TABLE IF NOT EXISTS kubernetes_configuration_key_mismatches (
    id                     INTEGER PRIMARY KEY,
    repository_id          INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    service_id             INTEGER REFERENCES services(id) ON DELETE SET NULL,
    environment_key        TEXT NOT NULL,
    source_kind            TEXT NOT NULL CHECK (source_kind IN ('config_map', 'secret')),
    source_name            TEXT NOT NULL,
    source_key             TEXT NOT NULL,
    reference_file_path    TEXT NOT NULL,
    reference_start_line   INTEGER NOT NULL,
    reference_end_line     INTEGER NOT NULL,
    declaration_file_path  TEXT NOT NULL,
    declaration_start_line INTEGER NOT NULL,
    declaration_end_line   INTEGER NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kubernetes_configuration_key_mismatches_repository
    ON kubernetes_configuration_key_mismatches(repository_id);
CREATE INDEX IF NOT EXISTS idx_kubernetes_configuration_key_mismatches_service
    ON kubernetes_configuration_key_mismatches(service_id);

-- A workload source with no matching ConfigMap/Secret declaration in indexed
-- plain YAML. It is an external-dependency hypothesis, never proof of absence.
CREATE TABLE IF NOT EXISTS kubernetes_configuration_source_unknowns (
    id                   INTEGER PRIMARY KEY,
    repository_id        INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    service_id           INTEGER REFERENCES services(id) ON DELETE SET NULL,
    environment_key      TEXT NOT NULL,
    source_kind          TEXT NOT NULL CHECK (source_kind IN ('config_map', 'secret')),
    source_name          TEXT NOT NULL,
    source_key           TEXT NOT NULL,
    reference_file_path  TEXT NOT NULL,
    reference_start_line INTEGER NOT NULL,
    reference_end_line   INTEGER NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kubernetes_configuration_source_unknowns_repository
    ON kubernetes_configuration_source_unknowns(repository_id);
CREATE INDEX IF NOT EXISTS idx_kubernetes_configuration_source_unknowns_service
    ON kubernetes_configuration_source_unknowns(service_id);

-- One row per class/controller/module cluster of endpoints within a service, synthesized
-- from the already-generated `apis` summaries of the endpoints it groups (never raw code
-- read again) — the layer between a single endpoint and the whole service. `file_path` is
-- the file the class/module lives in; `name` falls back to the file's stem when no class
-- wraps the endpoints (the common case for function-based routing, e.g. FastAPI/Flask).
CREATE TABLE IF NOT EXISTS components (
    id            INTEGER PRIMARY KEY,
    service_id    INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    summary       TEXT,
    evidence_json TEXT,
    input_digest  TEXT,
    updated_at    TEXT NOT NULL,
    UNIQUE(service_id, name, file_path)
);

CREATE INDEX IF NOT EXISTS idx_components_service ON components(service_id);

-- Deterministic execution context. These tables intentionally store a bounded
-- entrypoint-to-boundary flow, not an all-purpose code graph. They are populated
-- by local AST analyzers and optionally enriched by a depth provider.
CREATE TABLE IF NOT EXISTS entrypoints (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('http', 'graphql', 'grpc', 'message', 'cli', 'job', 'rpc')),
    method      TEXT NOT NULL,
    name        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(service_id, kind, method, name, symbol)
);

CREATE TABLE IF NOT EXISTS flow_edges (
    id            INTEGER PRIMARY KEY,
    service_id    INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    entrypoint_id INTEGER REFERENCES entrypoints(id) ON DELETE CASCADE,
    from_symbol   TEXT NOT NULL,
    to_symbol     TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('invokes', 'injects', 'validates', 'reads', 'writes', 'publishes', 'consumes')),
    confidence    TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    origin        TEXT NOT NULL CHECK (origin IN ('static', 'codegraph', 'runtime')),
    file_path     TEXT NOT NULL,
    start_line    INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entrypoints_service ON entrypoints(service_id);
CREATE INDEX IF NOT EXISTS idx_flow_edges_service ON flow_edges(service_id);
CREATE INDEX IF NOT EXISTS idx_flow_edges_entrypoint ON flow_edges(entrypoint_id);

-- Static error facts are deliberately metadata-only: no exception message,
-- response body or stack trace is persisted. A role records whether the source
-- symbol raises, handles or maps the error, allowing later deterministic smell
-- rules to distinguish a producer from a translation boundary.
CREATE TABLE IF NOT EXISTS static_error_contracts (
    id                      INTEGER PRIMARY KEY,
    service_id              INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    source                  TEXT NOT NULL,
    role                    TEXT NOT NULL CHECK (role IN ('raises', 'handles', 'maps')),
    error_kind              TEXT NOT NULL,
    internal_type           TEXT,
    protocol                TEXT NOT NULL CHECK (protocol IN ('http', 'grpc', 'graphql', 'internal')),
    transport_code          TEXT,
    public_code             TEXT,
    exposes_internal_detail INTEGER NOT NULL CHECK (exposes_internal_detail IN (0, 1)),
    retryability            TEXT NOT NULL CHECK (retryability IN ('retryable', 'not_retryable', 'unknown')),
    file_path               TEXT NOT NULL,
    start_line              INTEGER NOT NULL,
    end_line                INTEGER NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_static_error_contracts_service ON static_error_contracts(service_id);
CREATE INDEX IF NOT EXISTS idx_static_error_contracts_source ON static_error_contracts(service_id, source);

-- Static service calls are source-proven transport relations. The target is a
-- declared service identifier, not a runtime-discovered address or a guess.
CREATE TABLE IF NOT EXISTS static_service_calls (
    id              INTEGER PRIMARY KEY,
    service_id      INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    target_service  TEXT NOT NULL,
    protocol        TEXT NOT NULL CHECK (protocol IN ('http', 'grpc', 'graphql')),
    target_method   TEXT,
    target_path     TEXT,
    file_path       TEXT NOT NULL,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_static_service_calls_service ON static_service_calls(service_id);
CREATE INDEX IF NOT EXISTS idx_static_service_calls_source ON static_service_calls(service_id, source);

-- Literal resilience limits are stored separately from generic flow boundaries:
-- their numeric value and unit are evidence, not an inferred runtime guarantee.
CREATE TABLE IF NOT EXISTS static_resilience_policies (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('retry', 'timeout')),
    mechanism   TEXT NOT NULL CHECK (mechanism IN ('reactor', 'spring_annotation')),
    value       INTEGER NOT NULL CHECK (value >= 0),
    unit        TEXT NOT NULL CHECK (unit IN ('retries', 'attempts', 'milliseconds')),
    file_path   TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_static_resilience_policies_service ON static_resilience_policies(service_id);
CREATE INDEX IF NOT EXISTS idx_static_resilience_policies_source ON static_resilience_policies(service_id, source);

CREATE TABLE IF NOT EXISTS flow_boundaries (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('branch', 'async', 'retry', 'error', 'transaction')),
    file_path   TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entrypoint_contracts (
    entrypoint_id  INTEGER PRIMARY KEY REFERENCES entrypoints(id) ON DELETE CASCADE,
    contract_json  TEXT NOT NULL
);

-- Findings contain only a category, location and remediation guidance. Secret values
-- are deliberately never persisted in the knowledge base.
CREATE TABLE IF NOT EXISTS security_findings (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('tracked_dotenv', 'hardcoded_secret', 'cloud_credential_literal')),
    severity    TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    file_path   TEXT NOT NULL,
    line        INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(service_id, kind, file_path, line)
);

CREATE INDEX IF NOT EXISTS idx_security_findings_service ON security_findings(service_id);

CREATE TABLE IF NOT EXISTS indexed_files (
    id              INTEGER PRIMARY KEY,
    service_id      INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    file_path       TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    category        TEXT,
    last_indexed_at TEXT NOT NULL,
    UNIQUE(service_id, file_path)
);

-- Audit trail of find_change_surface calls, and the outcome feedback agents can
-- report back (record_change_surface_feedback), which recalibrate_confidence()
-- folds into future task inferences for that service.
CREATE TABLE IF NOT EXISTS change_surface_runs (
    id            INTEGER PRIMARY KEY,
    task_text     TEXT NOT NULL,
    backend       TEXT,
    created_at    TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL
);

-- Cached LLM synthesis only. The remaining change-surface facts are derived again
-- for every request, so a cache hit never returns stale graph/persistence data.
CREATE TABLE IF NOT EXISTS change_surface_synthesis_cache (
    cache_key   TEXT PRIMARY KEY,
    backend     TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_surface_findings (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES change_surface_runs(id) ON DELETE CASCADE,
    service       TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('primary', 'secondary', 'no_change', 'external_integration', 'unmapped_internal')),
    reason        TEXT,
    confidence    REAL,
    evidence_json TEXT
);

CREATE TABLE IF NOT EXISTS change_surface_feedback (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES change_surface_runs(id) ON DELETE CASCADE,
    service     TEXT NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN ('confirmed', 'rejected')),
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_surface_findings_run ON change_surface_findings(run_id);
CREATE INDEX IF NOT EXISTS idx_change_surface_findings_service ON change_surface_findings(service);
CREATE INDEX IF NOT EXISTS idx_change_surface_feedback_service ON change_surface_feedback(service);

-- Git ground-truth verification of a past find_change_surface run: what was
-- predicted (from change_surface_findings) vs. what actually changed according to
-- `git diff` against a repository, since a given commit. See generation/verification.py.
CREATE TABLE IF NOT EXISTS change_surface_verifications (
    id                    INTEGER PRIMARY KEY,
    run_id                INTEGER NOT NULL REFERENCES change_surface_runs(id) ON DELETE CASCADE,
    repository            TEXT NOT NULL,
    since_commit          TEXT NOT NULL,
    precision             REAL,
    recall                REAL,
    true_positives_json   TEXT NOT NULL,
    false_positives_json  TEXT NOT NULL,
    false_negatives_json  TEXT NOT NULL,
    verified_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_surface_verifications_run ON change_surface_verifications(run_id);

-- A plan is an auditable, bounded view over one change-surface run. It stores
-- metadata only; the task text remains owned by the existing surface-run record.
CREATE TABLE IF NOT EXISTS change_plan_runs (
    id                    INTEGER PRIMARY KEY,
    change_surface_run_id INTEGER REFERENCES change_surface_runs(id) ON DELETE SET NULL,
    status                TEXT NOT NULL CHECK (status IN ('ready', 'needs_decision', 'insufficient_evidence', 'stale_knowledge')),
    requested_tokens      INTEGER NOT NULL CHECK (requested_tokens > 0),
    estimated_tokens      INTEGER NOT NULL CHECK (estimated_tokens >= 0),
    token_measurement     TEXT NOT NULL DEFAULT 'byte_estimate',
    truncated             INTEGER NOT NULL CHECK (truncated IN (0, 1)),
    decision_points_json  TEXT NOT NULL DEFAULT '[]',
    selected_decisions_json TEXT NOT NULL DEFAULT '[]',
    change_units_json     TEXT NOT NULL DEFAULT '[]',
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_plan_runs_surface ON change_plan_runs(change_surface_run_id);

-- Agent-reported status for a safe CI command already indexed from GitHub Actions.
-- The command is copied as a source-proven audit reference because reindexing replaces
-- ci_commands. No stdout, stderr, arguments, notes or source content is retained.
CREATE TABLE IF NOT EXISTS change_plan_ci_validation_results (
    id            INTEGER PRIMARY KEY,
    plan_id       INTEGER NOT NULL REFERENCES change_plan_runs(id) ON DELETE CASCADE,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    workflow_path TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('test', 'build')),
    command       TEXT NOT NULL,
    start_line    INTEGER NOT NULL CHECK (start_line > 0),
    status        TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    duration_ms   INTEGER CHECK (duration_ms IS NULL OR duration_ms BETWEEN 0 AND 86400000),
    recorded_at   TEXT NOT NULL,
    UNIQUE(plan_id, repository_id, workflow_path, start_line)
);
CREATE INDEX IF NOT EXISTS idx_change_plan_ci_validation_results_plan
    ON change_plan_ci_validation_results(plan_id, repository_id);

-- Agent-reported state for a checklist item already persisted on a plan unit.
-- The check text lives only in change_units_json; no free-form result detail is stored.
CREATE TABLE IF NOT EXISTS change_plan_manual_validation_results (
    id             INTEGER PRIMARY KEY,
    plan_id        INTEGER NOT NULL REFERENCES change_plan_runs(id) ON DELETE CASCADE,
    change_unit_id TEXT NOT NULL,
    check_index    INTEGER NOT NULL CHECK (check_index >= 0),
    status         TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    recorded_at    TEXT NOT NULL,
    UNIQUE(plan_id, change_unit_id, check_index)
);
CREATE INDEX IF NOT EXISTS idx_change_plan_manual_validation_results_plan
    ON change_plan_manual_validation_results(plan_id, change_unit_id);

-- Latest-only numeric outcome from an explicit review_change_closure call.
-- Source paths, Git revisions, unit IDs and diff text never enter this table.
CREATE TABLE IF NOT EXISTS change_plan_closure_summaries (
    id                              INTEGER PRIMARY KEY,
    plan_id                         INTEGER NOT NULL REFERENCES change_plan_runs(id) ON DELETE CASCADE,
    repository_id                   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    status                          TEXT NOT NULL CHECK (status IN (
        'needs_attention', 'needs_review', 'ready_for_manual_review'
    )),
    planned_units                   INTEGER NOT NULL CHECK (planned_units >= 0),
    covered_units                   INTEGER NOT NULL CHECK (covered_units >= 0),
    omitted_units                   INTEGER NOT NULL CHECK (omitted_units >= 0),
    unassessable_units              INTEGER NOT NULL CHECK (unassessable_units >= 0),
    files_outside_planned_surface   INTEGER NOT NULL CHECK (files_outside_planned_surface >= 0),
    public_error_contracts_at_risk  INTEGER NOT NULL CHECK (public_error_contracts_at_risk >= 0),
    public_error_contract_breaks    INTEGER NOT NULL CHECK (public_error_contract_breaks >= 0),
    recorded_at                     TEXT NOT NULL,
    UNIQUE(plan_id, repository_id)
);
CREATE INDEX IF NOT EXISTS idx_change_plan_closure_summaries_repository
    ON change_plan_closure_summaries(repository_id, recorded_at);

-- Ephemeral local monitor state. Rows exist only while a bounded MCP operation
-- is executing; task text, arguments, source and results are never persisted.
CREATE TABLE IF NOT EXISTS local_activity_runs (
    id         INTEGER PRIMARY KEY,
    operation  TEXT NOT NULL CHECK (operation IN (
        'find_change_surface', 'get_change_context', 'plan_change', 'assess_working_change'
    )),
    process_id INTEGER NOT NULL,
    started_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_local_activity_runs_started ON local_activity_runs(started_at, id);

-- Aggregate-only progress for an index process. The local monitor reads it but
-- never writes; source paths, endpoint names and generated content stay out.
CREATE TABLE IF NOT EXISTS local_index_progress (
    id              INTEGER PRIMARY KEY,
    service         TEXT NOT NULL,
    process_id      INTEGER NOT NULL,
    total_units     INTEGER NOT NULL CHECK (total_units > 0),
    completed_units INTEGER NOT NULL CHECK (completed_units >= 0),
    stage           TEXT NOT NULL CHECK (stage IN (
        'discovery', 'endpoint_analysis', 'component_analysis',
        'persistence_analysis', 'messaging_analysis', 'overview_generation'
    )),
    started_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(process_id, service)
);
CREATE INDEX IF NOT EXISTS idx_local_index_progress_started ON local_index_progress(started_at, id);

-- A static snapshot is valid only for the exact analyzer input digest and parser
-- version. It stores no source content and is invalidated by external depth facts.
CREATE TABLE IF NOT EXISTS static_analysis_snapshots (
    service_id       INTEGER PRIMARY KEY REFERENCES services(id) ON DELETE CASCADE,
    input_digest     TEXT NOT NULL,
    analysis_version TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Canonical static facts for one completed analysis, including an empty snapshot.
-- Source references and derived metadata are stored without source code content.
CREATE TABLE IF NOT EXISTS canonical_snapshots (
    service_id       INTEGER PRIMARY KEY REFERENCES services(id) ON DELETE CASCADE,
    service_name     TEXT NOT NULL,
    repository_name  TEXT,
    payload_json     TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Privacy-safe calibration data for get_change_context. These rows deliberately
-- contain no task/prompt text, source/code excerpts or generated card text: only
-- response measurements, service IDs and bounded tool metadata.
CREATE TABLE IF NOT EXISTS context_budget_runs (
    id                         INTEGER PRIMARY KEY,
    change_surface_run_id      INTEGER REFERENCES change_surface_runs(id) ON DELETE CASCADE,
    repository_id              INTEGER REFERENCES repositories(id) ON DELETE SET NULL,
    epic_type                  TEXT NOT NULL,
    requested_budget           INTEGER NOT NULL CHECK (requested_budget BETWEEN 1 AND 5),
    returned_cards             INTEGER NOT NULL,
    candidate_count            INTEGER NOT NULL,
    truncated                  INTEGER NOT NULL CHECK (truncated IN (0, 1)),
    response_bytes             INTEGER NOT NULL,
    estimated_tokens           INTEGER NOT NULL,
    token_measurement          TEXT NOT NULL DEFAULT 'byte_estimate',
    included_service_ids_json  TEXT NOT NULL,
    omitted_service_ids_json   TEXT NOT NULL,
    candidate_ranking_json     TEXT NOT NULL,
    recommended_queries_json   TEXT NOT NULL,
    created_at                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_budget_feedback (
    id                       INTEGER PRIMARY KEY,
    context_run_id           INTEGER NOT NULL REFERENCES context_budget_runs(id) ON DELETE CASCADE,
    outcome                  TEXT NOT NULL CHECK (outcome IN ('sufficient', 'insufficient', 'excessive')),
    note_digest              TEXT,
    missing_service_ids_json TEXT NOT NULL,
    recorded_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_budget_query_executions (
    id             INTEGER PRIMARY KEY,
    context_run_id INTEGER NOT NULL REFERENCES context_budget_runs(id) ON DELETE CASCADE,
    tool           TEXT NOT NULL,
    service_id     INTEGER REFERENCES services(id) ON DELETE SET NULL,
    executed_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_context_budget_runs_surface ON context_budget_runs(change_surface_run_id);
CREATE INDEX IF NOT EXISTS idx_context_budget_feedback_run ON context_budget_feedback(context_run_id);
CREATE INDEX IF NOT EXISTS idx_context_budget_executions_run ON context_budget_query_executions(context_run_id);

CREATE TABLE IF NOT EXISTS context_budget_verifications (
    id                       INTEGER PRIMARY KEY,
    context_run_id           INTEGER NOT NULL REFERENCES context_budget_runs(id) ON DELETE CASCADE,
    repository               TEXT NOT NULL,
    since_commit             TEXT NOT NULL,
    precision                REAL,
    recall                   REAL,
    omission_rate            REAL,
    actual_service_ids_json  TEXT NOT NULL,
    verified_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_context_budget_verifications_run ON context_budget_verifications(context_run_id);

CREATE TABLE IF NOT EXISTS index_runs (
    id            INTEGER PRIMARY KEY,
    service_id    INTEGER REFERENCES services(id) ON DELETE SET NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT CHECK (status IN ('ok', 'partial', 'failed')),
    backend       TEXT,
    files_changed INTEGER DEFAULT 0,
    llm_calls     INTEGER DEFAULT 0,
    llm_invocations INTEGER,
    notes         TEXT,
    -- Best-effort token/cost accounting summed across every unit generated in this
    -- run (see generation.backend_base.LLMUsage) — NULL, never a guess, whenever the
    -- backend's CLI output didn't carry it. See generation/llm_harness.py.
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL
);

-- Aggregate indexing usage by generation kind. No route, file, prompt or code
-- identifiers are stored here; old runs have no rows instead of invented zeros.
CREATE TABLE IF NOT EXISTS index_run_unit_usage (
    run_id          INTEGER NOT NULL REFERENCES index_runs(id) ON DELETE CASCADE,
    unit_kind       TEXT NOT NULL CHECK (unit_kind IN ('endpoint', 'component', 'persistence', 'messaging', 'overview')),
    generated_units INTEGER NOT NULL CHECK (generated_units >= 0),
    llm_invocations INTEGER NOT NULL CHECK (llm_invocations >= 0),
    had_failure     INTEGER NOT NULL CHECK (had_failure IN (0, 1)),
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost_usd        REAL,
    backend_duration_ms REAL CHECK (backend_duration_ms >= 0),
    PRIMARY KEY (run_id, unit_kind)
);

-- Per-unit measurements. unit_key is a database-local HMAC; no route, file,
-- prompt, symbol or source text is stored in this table.
CREATE TABLE IF NOT EXISTS index_run_units (
    run_id              INTEGER NOT NULL REFERENCES index_runs(id) ON DELETE CASCADE,
    unit_kind           TEXT NOT NULL CHECK (unit_kind IN ('endpoint', 'component', 'persistence', 'messaging', 'overview')),
    unit_key            TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('skipped', 'success', 'failed')),
    llm_invocations     INTEGER NOT NULL CHECK (llm_invocations >= 0),
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cached_input_tokens INTEGER,
    cost_usd            REAL,
    backend_duration_ms REAL NOT NULL CHECK (backend_duration_ms >= 0),
    PRIMARY KEY (run_id, unit_kind, unit_key)
);

-- Deterministic, whole-graph structural findings and bounded flow hypotheses (cycles,
-- fan-in/out imbalance, shared database, duplicate external integration, BFF-policy
-- and non-atomic-publish candidates) recomputed after every index/update from
-- already-indexed facts alone — no LLM call. See generation/architecture.py. Versioned
-- per run, the same way change_surface_runs is, so findings are comparable over time
-- (e.g. is a monolith's fan-in shrinking as a strangler-fig migration progresses).
CREATE TABLE IF NOT EXISTS architecture_runs (
    id               INTEGER PRIMARY KEY,
    created_at       TEXT NOT NULL,
    services_indexed INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS architecture_findings (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES architecture_runs(id) ON DELETE CASCADE,
    -- Finding categories evolve with deterministic detectors; a fixed enum would
    -- force a destructive table rebuild for every new evidence-backed signal.
    kind          TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')) DEFAULT 'info',
    services_json TEXT NOT NULL,
    detail_json   TEXT,
    reason        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_architecture_findings_run ON architecture_findings(run_id);
CREATE INDEX IF NOT EXISTS idx_architecture_findings_kind ON architecture_findings(kind);

-- Runtime observations are deliberately separate from static flow_edges. They hold
-- normalized symbols and counters only: no trace/span IDs, attributes, payloads or
-- source excerpts are retained.
CREATE TABLE IF NOT EXISTS runtime_flow_observations (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    source      TEXT NOT NULL CHECK (source IN ('otel', 'broker')),
    from_symbol TEXT NOT NULL,
    to_symbol   TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('invokes', 'injects', 'validates', 'reads', 'writes', 'publishes', 'consumes')),
    observed_count INTEGER NOT NULL CHECK (observed_count > 0),
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE(service_id, source, from_symbol, to_symbol, kind)
);

CREATE INDEX IF NOT EXISTS idx_runtime_flow_service ON runtime_flow_observations(service_id);

-- Full-text search index, populated explicitly by repository.rebuild_search_index*
-- (not kept in sync via triggers — every write path in this project already replaces
-- rows in bulk per service, so an explicit rebuild after each service's writes is
-- simpler and cheap at this project's scale). content_text is the only indexed
-- column; the rest are just retrieved back on a match, not searched.
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    kind UNINDEXED,
    service UNINDEXED,
    ref UNINDEXED,
    snippet UNINDEXED,
    content_text,
    service_id UNINDEXED
);

-- Local, zero-marginal-cost semantic vectors (see generation/embeddings.py) used as
-- a fallback when FTS5 keyword retrieval (search_fts) finds nothing for a task's
-- vocabulary, and to rank similar past find_change_surface tasks. vector_json is a
-- JSON array of floats — brute-force cosine similarity in Python is plenty fast at
-- this project's catalog scale, so no vector-DB dependency or BLOB packing is
-- introduced for it. Both tables are the same "vector storage for X" concern for two
-- different aggregates, read/written by the single db/repositories/embeddings.py.
CREATE TABLE IF NOT EXISTS service_embeddings (
    service_id  INTEGER PRIMARY KEY REFERENCES services(id) ON DELETE CASCADE,
    model_name  TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_surface_run_embeddings (
    run_id      INTEGER PRIMARY KEY REFERENCES change_surface_runs(id) ON DELETE CASCADE,
    model_name  TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_apis_service ON apis(service_id);
CREATE INDEX IF NOT EXISTS idx_service_calls_from ON service_calls(from_service_id);
CREATE INDEX IF NOT EXISTS idx_service_calls_to_name ON service_calls(to_service_name);
CREATE INDEX IF NOT EXISTS idx_service_calls_to_id ON service_calls(to_service_id);
CREATE INDEX IF NOT EXISTS idx_persistence_service ON persistence_entities(service_id);
CREATE INDEX IF NOT EXISTS idx_static_migration_facts_service ON static_migration_facts(service_id);
CREATE INDEX IF NOT EXISTS idx_messages_service ON messages(service_id);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel);
CREATE INDEX IF NOT EXISTS idx_indexed_files_service ON indexed_files(service_id);
CREATE INDEX IF NOT EXISTS idx_services_repository ON services(repository_id);

CREATE TABLE IF NOT EXISTS service_index_locks (
    lock_key    TEXT PRIMARY KEY,
    acquired_at TEXT NOT NULL,
    process_id  INTEGER
);
