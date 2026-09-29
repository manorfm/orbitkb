"""Projects current static analysis onto language-neutral facts."""

import json
from dataclasses import replace

from orbitkb.analysis.models import AnalysisResult, Evidence
from orbitkb.domain.canonical import (
    CanonicalFact,
    CanonicalSnapshot,
    CloudResourceKey,
    EntrypointKey,
    FactStatus,
    MessageChannelKey,
    PersistenceResourceKey,
    RoutePatternKey,
    ServiceKey,
    SourceReference,
    SymbolKey,
    fact_id,
)


def _add_fact(facts: dict[str, CanonicalFact], fact: CanonicalFact) -> None:
    previous = facts.get(fact.id)
    if previous is None:
        facts[fact.id] = fact
        return
    if (previous.kind, previous.subject, previous.attributes, previous.status, previous.origin) != (
        fact.kind, fact.subject, fact.attributes, fact.status, fact.origin,
    ):
        raise ValueError(f"conflicting values for canonical fact {fact.id}")
    new_sources = tuple(source for source in fact.sources if source not in previous.sources)
    if new_sources:
        facts[fact.id] = replace(previous, sources=(*previous.sources, *new_sources))


def _source(evidence: Evidence) -> SourceReference:
    return SourceReference(evidence.file_path, evidence.start_line, evidence.end_line)


def project_analysis(service: ServiceKey, analysis: AnalysisResult) -> CanonicalSnapshot:
    """Project source-proven facts without altering legacy storage."""
    facts: dict[str, CanonicalFact] = {}
    for entry in analysis.entrypoints:
        key = EntrypointKey(service, entry.kind, entry.method, entry.name, entry.symbol)
        contract = entry.contract or analysis.contracts.get(entry.symbol)
        attributes = {"contract": contract}
        sources = [_source(entry.evidence)]
        response = contract.get("returns") if isinstance(contract, dict) else None
        for evidence_key in ("derived_from", "receiver_evidence"):
            derivation = response.get(evidence_key) if isinstance(response, dict) else None
            if isinstance(derivation, dict) and isinstance(derivation.get("file"), str) and (
                isinstance(derivation.get("start_line"), int) and derivation["start_line"] > 0
            ):
                sources.append(SourceReference(
                    derivation["file"], derivation["start_line"], derivation["start_line"],
                ))
        _add_fact(facts, CanonicalFact(
            id=key.fact_id, kind="entrypoint", subject=key, attributes=attributes,
            status=FactStatus.CONFIRMED, origin="static", sources=tuple(sources),
        ))
    for symbol in analysis.symbols:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "symbol", symbol.name, symbol.owner, symbol.member),
            kind="symbol", subject=SymbolKey(service, symbol.name),
            attributes={"owner": symbol.owner, "member": symbol.member, "implements": symbol.implements,
                        "imports": symbol.imports, "qualifiers": symbol.qualifiers, "primary": symbol.primary},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(symbol.evidence),),
        ))
    for injection in analysis.injections:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "injection", injection.consumer, injection.contract, injection.qualifier),
            kind="injection", subject=SymbolKey(service, injection.consumer),
            attributes={"contract": injection.contract, "qualifier": injection.qualifier},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(injection.evidence),),
        ))
    for boundary in analysis.boundaries:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "flow_boundary", boundary.source, boundary.kind),
            kind="flow_boundary", subject=SymbolKey(service, boundary.source),
            attributes={"boundary_kind": boundary.kind},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(boundary.evidence),),
        ))
    for edge in analysis.edges:
        source = _source(edge.evidence)
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "flow_edge", edge.source, edge.target, edge.kind, edge.origin, edge.confidence,
                       *((edge.boundary_kind,) if edge.boundary_kind else ())),
            kind="flow_edge", subject=SymbolKey(service, edge.source),
            attributes={"relation": edge.kind, "target": edge.target, "confidence": edge.confidence,
                        **({"boundary_kind": edge.boundary_kind} if edge.boundary_kind else {})},
            status=(FactStatus.CONFIRMED if edge.origin == "static" and edge.confidence == "high"
                    else FactStatus.INFERRED),
            origin=edge.origin, sources=(source,),
        ))
    for call in analysis.static_service_calls:
        source = _source(call.evidence)
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "service_call", call.source, call.target_service, call.protocol,
                       call.target_method or "", call.target_path or ""),
            kind="service_call", subject=SymbolKey(service, call.source),
            attributes={"target_service": call.target_service, "protocol": call.protocol,
                        "target_method": call.target_method, "target_path": call.target_path},
            status=FactStatus.CONFIRMED, origin="static", sources=(source,),
        ))
    for binding in analysis.configuration_bindings:
        source = _source(binding.evidence)
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "configuration", binding.source, binding.key, binding.kind, binding.sensitive),
            kind="configuration", subject=SymbolKey(service, binding.source),
            attributes={"key": binding.key, "binding_kind": binding.kind, "sensitive": binding.sensitive},
            status=FactStatus.CONFIRMED, origin="static", sources=(source,),
        ))
    for requirement in analysis.security_requirements:
        if (requirement.route_pattern is None) == (requirement.symbol is None) or (
            requirement.symbol is not None and requirement.method is not None
        ):
            raise ValueError("security requirement subject must be either a route pattern or a symbol")
        if requirement.symbol is not None:
            subject = SymbolKey(service, requirement.symbol)
            identity = ("symbol", requirement.symbol)
        else:
            subject = RoutePatternKey(service, requirement.method, requirement.route_pattern)
            identity = ("route", requirement.method, requirement.route_pattern)
        source = _source(requirement.evidence)
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "security_requirement", *identity, requirement.requirement,
                       json.dumps(requirement.roles)),
            kind="security_requirement", subject=subject,
            attributes={"requirement": requirement.requirement, "roles": requirement.roles},
            status=(FactStatus.UNKNOWN if requirement.requirement.startswith("custom:") else FactStatus.CONFIRMED),
            origin="static", sources=(source,),
        ))
    for error in analysis.error_contracts:
        source = _source(error.evidence)
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "error_contract", error.source, error.role, error.error_kind, error.internal_type,
                       error.protocol, error.transport_code, error.public_code, error.exposes_internal_detail,
                       error.retryability),
            kind="error_contract", subject=SymbolKey(service, error.source),
            attributes={"role": error.role, "error_kind": error.error_kind, "internal_type": error.internal_type,
                        "protocol": error.protocol, "transport_code": error.transport_code,
                        "public_code": error.public_code, "exposes_internal_detail": error.exposes_internal_detail,
                        "retryability": error.retryability},
            status=FactStatus.CONFIRMED, origin="static", sources=(source,),
        ))
    for message in analysis.message_contracts:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "message_contract", message.direction, message.channel, message.routing_key,
                       message.payload_type, message.message_version),
            kind="message_contract", subject=MessageChannelKey(service, message.channel),
            attributes={"direction": message.direction, "routing_key": message.routing_key,
                        "payload_type": message.payload_type, "message_version": message.message_version},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(message.evidence),),
        ))
    for resource in analysis.persistence_facts:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "persistence_resource", resource.kind, resource.name, resource.owner),
            kind="persistence_resource", subject=PersistenceResourceKey(service, resource.kind, resource.name),
            attributes={"owner": resource.owner},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(resource.evidence),),
        ))
    for migration in analysis.migration_facts:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "migration", migration.table_name, migration.operation, migration.column_name,
                       migration.destructive),
            kind="migration", subject=PersistenceResourceKey(service, "sql_table", migration.table_name),
            attributes={"operation": migration.operation, "column_name": migration.column_name,
                        "destructive": migration.destructive},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(migration.evidence),),
        ))
    for handler in analysis.grpc_handlers:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "grpc_handler", handler.service, handler.rpc, handler.symbol),
            kind="grpc_handler", subject=SymbolKey(service, handler.symbol),
            attributes={"grpc_service": handler.service, "rpc": handler.rpc},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(handler.evidence),),
        ))
    for binding in analysis.grpc_client_bindings:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "grpc_client_binding", binding.owner, binding.member, binding.service),
            kind="grpc_client_binding", subject=SymbolKey(service, binding.owner),
            attributes={"member": binding.member, "grpc_service": binding.service},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(binding.evidence),),
        ))
    for policy in analysis.resilience_policies:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "resilience_policy", policy.source, policy.kind, policy.mechanism,
                       policy.value, policy.unit),
            kind="resilience_policy", subject=SymbolKey(service, policy.source),
            attributes={"policy_kind": policy.kind, "mechanism": policy.mechanism,
                        "value": policy.value, "unit": policy.unit},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(policy.evidence),),
        ))
    for flag in analysis.feature_flags:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "feature_flag", flag.source, flag.key, flag.provider),
            kind="feature_flag", subject=SymbolKey(service, flag.source),
            attributes={"key": flag.key, "provider": flag.provider},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(flag.evidence),),
        ))
    for cloud in analysis.cloud_facts:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "cloud_operation", cloud.provider, cloud.resource_type, cloud.service_name,
                       cloud.operation, cloud.operation_kind, cloud.sdk, cloud.target_name),
            kind="cloud_operation",
            subject=CloudResourceKey(service, cloud.provider, cloud.resource_type, cloud.target_name),
            attributes={"service_name": cloud.service_name, "operation": cloud.operation,
                        "operation_kind": cloud.operation_kind, "sdk": cloud.sdk},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(cloud.evidence),),
        ))
    for header in analysis.api_headers:
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "api_header", header.method, header.path, header.direction, header.name),
            kind="api_header", subject=RoutePatternKey(service, header.method, header.path),
            attributes={"direction": header.direction, "name": header.name},
            status=FactStatus.CONFIRMED, origin="static", sources=(_source(header.evidence),),
        ))
    return CanonicalSnapshot(service, tuple(facts.values()))
