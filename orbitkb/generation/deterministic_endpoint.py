"""Render the narrow, fully evidenced local HTTP endpoint shape without inference."""

from __future__ import annotations

import jsonschema

from orbitkb.domain.canonical import FactStatus
from orbitkb.domain.reduction import ContextCapsule
from orbitkb.domain.sufficiency import SufficiencyResult, SufficiencyStatus
from orbitkb.generation.llm_harness import load_schema


def render_simple_endpoint(capsule: ContextCapsule, assessment: SufficiencyResult) -> dict | None:
    """Return an API detail only for a direct, bodyless, public GET with no calls."""
    if (assessment.overall != SufficiencyStatus.ENOUGH or len(capsule.entrypoints) != 1
            or capsule.entrypoints[0].transport != "http" or capsule.entrypoints[0].method != "GET"
            or capsule.truncated or capsule.boundaries):
        return None
    entries = [fact for fact in capsule.facts if fact.kind == "entrypoint"]
    security = [fact for fact in capsule.facts if fact.kind == "security_requirement"]
    if (len(entries) != 1 or len(security) != 1
            or any(fact.kind in {"flow_edge", "service_call"} for fact in capsule.facts)
            or any(fact.status != FactStatus.CONFIRMED or fact.origin != "static"
                   for fact in (*entries, *security))
            or security[0].value.get("requirement") != "permitAll"
            or security[0].value.get("route_pattern") != capsule.entrypoints[0].name
            or security[0].value.get("method") not in (None, "GET")):
        return None
    contract = entries[0].value.get("contract")
    if (not isinstance(contract, dict) or contract.get("request") is not None
            or contract.get("validations") or contract.get("authorization")):
        return None
    formal = contract.get("formal_contract")
    response = contract.get("returns")
    if (not isinstance(formal, dict) or formal.get("format") != "openapi"
            or formal.get("request_body_present") is not False
            or formal.get("security") not in {"unspecified", "not_required"}
            or not isinstance(response, dict) or not isinstance(response.get("fields"), list)
            or not response["fields"]):
        return None
    summary, description = formal.get("summary"), formal.get("description")
    if not all(isinstance(value, str) and value.strip() for value in (summary, description)):
        return None
    fields = response["fields"]
    if any(not isinstance(field, dict) or not isinstance(field.get("name"), str)
           or not field["name"].strip() or not isinstance(field.get("type"), str)
           or not field["type"].strip() for field in fields):
        return None
    document = {
        "summary": summary,
        "description": description,
        "response_shape": [{"field": field["name"], "type_desc": field["type"]} for field in fields],
        "request_shape": [],
        "calls": [],
        "validations": [{"kind": "authorization", "description": "Public access is permitted."}],
    }
    return document if jsonschema.Draft202012Validator(load_schema("api_detail")).is_valid(document) else None
