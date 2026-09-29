from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Evidence:
    file_path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class EntryPoint:
    """A concrete way work enters a service, independent of transport."""

    kind: str
    method: str
    name: str
    symbol: str
    evidence: Evidence
    contract: dict | None = None


@dataclass(frozen=True)
class FlowEdge:
    """A deterministic relation observed in a bounded entrypoint flow."""

    source: str
    target: str
    kind: str
    evidence: Evidence
    confidence: str = "high"
    origin: str = "static"
    boundary_kind: str | None = None


@dataclass(frozen=True)
class Symbol:
    """A locally declared callable used only for bounded flow resolution."""

    name: str
    owner: str
    member: str
    evidence: Evidence
    implements: tuple[str, ...] = ()
    imports: tuple[tuple[str, str], ...] = ()
    qualifiers: tuple[str, ...] = ()
    primary: bool = False


@dataclass(frozen=True)
class Injection:
    """A declared dependency selection used only for bounded resolution."""

    consumer: str
    contract: str
    qualifier: str | None
    evidence: Evidence


@dataclass(frozen=True)
class MessageContract:
    direction: str
    channel: str
    routing_key: str | None
    payload_type: str | None
    evidence: Evidence
    message_version: str | None = None


@dataclass(frozen=True)
class FlowBoundary:
    source: str
    kind: str
    evidence: Evidence


@dataclass(frozen=True)
class ErrorContract:
    """A source-proven error raised, handled or mapped by one local symbol.

    This carries metadata only: exception messages, payloads and stack traces are
    deliberately excluded from the static knowledge model.
    """

    source: str
    role: str
    error_kind: str
    internal_type: str | None
    protocol: str
    transport_code: str | None
    public_code: str | None
    exposes_internal_detail: bool
    retryability: str
    evidence: Evidence


@dataclass(frozen=True)
class StaticServiceCall:
    """A locally proven call to a named service and transport endpoint."""

    source: str
    target_service: str
    protocol: str
    target_method: str | None
    target_path: str | None
    evidence: Evidence


@dataclass(frozen=True)
class GrpcHandler:
    """A static handler explicitly bound to a literal Protobuf service/RPC name."""

    service: str
    rpc: str
    symbol: str
    evidence: Evidence


@dataclass(frozen=True)
class GrpcClientBinding:
    """A direct generated or Nest gRPC service-stub binding."""

    owner: str
    member: str
    service: str
    evidence: Evidence


@dataclass(frozen=True)
class ResiliencePolicy:
    """A literal timeout or retry limit declared by a local source symbol."""

    source: str
    kind: str
    mechanism: str
    value: int
    unit: str
    evidence: Evidence


@dataclass(frozen=True)
class PersistenceFact:
    name: str
    kind: str
    owner: str
    evidence: Evidence


@dataclass(frozen=True)
class MigrationFact:
    """A literal, local SQL migration operation; no execution state is inferred."""

    operation: str
    table_name: str
    column_name: str | None
    destructive: bool
    evidence: Evidence


@dataclass(frozen=True)
class ConfigurationBinding:
    """A literal configuration key read by one local symbol, never its value."""

    source: str
    key: str
    kind: str
    sensitive: bool
    evidence: Evidence


@dataclass(frozen=True)
class FeatureFlag:
    """A literal flag key read through a locally proven feature-flag SDK."""

    source: str
    key: str
    provider: str
    evidence: Evidence


@dataclass(frozen=True)
class CloudFact:
    """A cloud SDK operation proven by locally-declared type or import — see
    orbitkb/analysis/cloud_taxonomy.py for the vendor-sourced vocabulary this
    draws from. `target_name` is the literal queue/bucket/topic name only when
    the call site names it; `None` means unresolved, never a guess."""

    provider: str
    resource_type: str
    service_name: str
    operation: str
    operation_kind: str
    sdk: str
    target_name: str | None
    evidence: Evidence


@dataclass(frozen=True)
class ApiHeader:
    """One HTTP header name a specific route reads (`@RequestHeader`) or writes
    (a `ResponseEntity` header builder call) -- the name only, never a value:
    a request header's value is caller-supplied runtime data, and a response
    header's value is frequently itself computed, so neither is a literal,
    indexable fact the way the header's name is."""

    method: str
    path: str
    direction: str
    name: str
    evidence: Evidence


@dataclass(frozen=True)
class SecurityRequirement:
    """One authorization rule proven either for a route pattern (a Spring Security
    `SecurityFilterChain`'s `authorizeHttpRequests` block) or for a specific symbol
    (`@PreAuthorize`/`@Secured`) -- exactly one of `route_pattern` or `symbol` is
    set, never both. Deliberately not a boolean formula: `roles` lists every role
    literal the requirement's own code names (resolving one hop into a local
    policy function when the requirement is a custom `AuthorizationManager`), not
    a proof of how they combine -- an arbitrary custom `AuthorizationManager`'s
    actual runtime logic is not a local, deterministic fact, so it's surfaced as
    `requirement="custom:<ClassName>"` with whatever roles could be resolved
    rather than guessed at further."""

    route_pattern: str | None
    method: str | None
    symbol: str | None
    requirement: str
    roles: tuple[str, ...]
    evidence: Evidence


@dataclass
class AnalysisResult:
    entrypoints: list[EntryPoint] = field(default_factory=list)
    edges: list[FlowEdge] = field(default_factory=list)
    contracts: dict[str, dict] = field(default_factory=dict)
    symbols: list[Symbol] = field(default_factory=list)
    injections: list[Injection] = field(default_factory=list)
    message_contracts: list[MessageContract] = field(default_factory=list)
    boundaries: list[FlowBoundary] = field(default_factory=list)
    error_contracts: list[ErrorContract] = field(default_factory=list)
    static_service_calls: list[StaticServiceCall] = field(default_factory=list)
    grpc_handlers: list[GrpcHandler] = field(default_factory=list)
    grpc_client_bindings: list[GrpcClientBinding] = field(default_factory=list)
    resilience_policies: list[ResiliencePolicy] = field(default_factory=list)
    persistence_facts: list[PersistenceFact] = field(default_factory=list)
    migration_facts: list[MigrationFact] = field(default_factory=list)
    configuration_bindings: list[ConfigurationBinding] = field(default_factory=list)
    feature_flags: list[FeatureFlag] = field(default_factory=list)
    cloud_facts: list[CloudFact] = field(default_factory=list)
    security_requirements: list[SecurityRequirement] = field(default_factory=list)
    api_headers: list[ApiHeader] = field(default_factory=list)

    def extend(self, other: AnalysisResult) -> None:
        self.entrypoints.extend(other.entrypoints)
        self.edges.extend(other.edges)
        self.contracts.update(other.contracts)
        self.symbols.extend(other.symbols)
        self.injections.extend(other.injections)
        self.message_contracts.extend(other.message_contracts)
        self.boundaries.extend(other.boundaries)
        self.error_contracts.extend(other.error_contracts)
        self.static_service_calls.extend(other.static_service_calls)
        self.grpc_handlers.extend(other.grpc_handlers)
        self.grpc_client_bindings.extend(other.grpc_client_bindings)
        self.resilience_policies.extend(other.resilience_policies)
        self.persistence_facts.extend(other.persistence_facts)
        self.migration_facts.extend(other.migration_facts)
        self.configuration_bindings.extend(other.configuration_bindings)
        self.feature_flags.extend(other.feature_flags)
        self.cloud_facts.extend(other.cloud_facts)
        self.security_requirements.extend(other.security_requirements)
        self.api_headers.extend(other.api_headers)
