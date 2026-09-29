"""Projects existing static entrypoints onto language-neutral facts."""

from dataclasses import replace

from orbitkb.analysis.models import AnalysisResult
from orbitkb.domain.canonical import (
    CanonicalFact,
    CanonicalSnapshot,
    EntrypointKey,
    FactStatus,
    ServiceKey,
    SourceReference,
)


def project_entrypoints(service: ServiceKey, analysis: AnalysisResult) -> CanonicalSnapshot:
    """Project proven entrypoints without changing the legacy analysis or storage."""
    facts: dict[EntrypointKey, CanonicalFact] = {}
    for entry in analysis.entrypoints:
        key = EntrypointKey(service, entry.kind, entry.method, entry.name, entry.symbol)
        attributes = {"contract": entry.contract or analysis.contracts.get(entry.symbol)}
        source = SourceReference(entry.evidence.file_path, entry.evidence.start_line, entry.evidence.end_line)
        if key in facts:
            previous = facts[key]
            if previous.attributes != attributes:
                raise ValueError(f"conflicting contracts for entrypoint {previous.id}")
            if source not in previous.sources:
                facts[key] = replace(previous, sources=(*previous.sources, source))
            continue
        facts[key] = CanonicalFact(
            id=key.fact_id, kind="entrypoint", subject=key, attributes=attributes,
            status=FactStatus.CONFIRMED, origin="static", sources=(source,),
        )
    return CanonicalSnapshot(service, tuple(facts.values()))
