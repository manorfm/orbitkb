"""Projects current static analysis onto language-neutral facts."""

from dataclasses import replace

from orbitkb.analysis.models import AnalysisResult
from orbitkb.domain.canonical import (
    CanonicalFact,
    CanonicalSnapshot,
    EntrypointKey,
    FactStatus,
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


def project_analysis(service: ServiceKey, analysis: AnalysisResult) -> CanonicalSnapshot:
    """Project proven entrypoints and relations without altering legacy storage."""
    facts: dict[str, CanonicalFact] = {}
    for entry in analysis.entrypoints:
        key = EntrypointKey(service, entry.kind, entry.method, entry.name, entry.symbol)
        attributes = {"contract": entry.contract or analysis.contracts.get(entry.symbol)}
        source = SourceReference(entry.evidence.file_path, entry.evidence.start_line, entry.evidence.end_line)
        _add_fact(facts, CanonicalFact(
            id=key.fact_id, kind="entrypoint", subject=key, attributes=attributes,
            status=FactStatus.CONFIRMED, origin="static", sources=(source,),
        ))
    for edge in analysis.edges:
        source = SourceReference(edge.evidence.file_path, edge.evidence.start_line, edge.evidence.end_line)
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "flow_edge", edge.source, edge.target, edge.kind, edge.origin, edge.confidence),
            kind="flow_edge", subject=SymbolKey(service, edge.source),
            attributes={"relation": edge.kind, "target": edge.target, "confidence": edge.confidence},
            status=(FactStatus.CONFIRMED if edge.origin == "static" and edge.confidence == "high"
                    else FactStatus.INFERRED),
            origin=edge.origin, sources=(source,),
        ))
    for call in analysis.static_service_calls:
        source = SourceReference(call.evidence.file_path, call.evidence.start_line, call.evidence.end_line)
        _add_fact(facts, CanonicalFact(
            id=fact_id(service, "service_call", call.source, call.target_service, call.protocol,
                       call.target_method or "", call.target_path or ""),
            kind="service_call", subject=SymbolKey(service, call.source),
            attributes={"target_service": call.target_service, "protocol": call.protocol,
                        "target_method": call.target_method, "target_path": call.target_path},
            status=FactStatus.CONFIRMED, origin="static", sources=(source,),
        ))
    return CanonicalSnapshot(service, tuple(facts.values()))
