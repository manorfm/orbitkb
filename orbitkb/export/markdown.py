from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import canonical_snapshots as snapshots_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.domain.navigation import KnowledgeNavigator
from orbitkb.domain.route_calls import RouteCallStatus, route_declared_http_calls
from orbitkb.export.dependencies import unresolved_declared_http_targets
from orbitkb.export.messaging import has_confirmed_redis_publication
from orbitkb.export.persistence import has_unrepresented_mongo_access


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower() or "root"


def _fmt_calls(calls: list[sqlite3.Row]) -> list[str]:
    lines = []
    for c in calls:
        data_needed = ", ".join(json.loads(c["data_needed"] or "[]"))
        call_kind = c["call_kind"] + (" (unresolved)" if c["target_kind"] == "unknown" else "")
        line = f"- **{c['to_service_name']}** ({call_kind}, {c['purpose_kind']}): {c['reason']}"
        if data_needed:
            line += f" — needs: {data_needed}"
        lines.append(line)
    return lines or ["- (no dependency detected)"]


def _fmt_messages(messages: list[sqlite3.Row]) -> list[str]:
    lines = [f"- **{m['channel']}** ({m['direction']}): {m['description'] or ''}" for m in messages]
    return lines or ["- (none detected)"]


def export_markdown(conn: sqlite3.Connection, out_dir: Path, service_filter: str | None = None) -> list[Path]:
    written: list[Path] = []
    for svc in services_repo.list_services(conn):
        if service_filter and svc["name"] != service_filter:
            continue
        service_row = services_repo.get_service_by_name(conn, svc["name"])
        service_dir = out_dir / svc["name"]
        service_dir.mkdir(parents=True, exist_ok=True)

        calls = service_calls_repo.list_calls_for_service(conn, svc["id"])
        declared_targets = unresolved_declared_http_targets(
            flows_repo.list_static_service_calls(conn, svc["id"]),
            (call["to_service_name"] for call in calls),
        )
        apis = apis_repo.list_apis(conn, svc["id"])
        persistence = persistence_repo.list_persistence(conn, svc["id"])
        snapshot = snapshots_repo.read_snapshot(conn, svc["id"])
        navigator = KnowledgeNavigator(snapshot) if snapshot is not None else None
        messages = messages_repo.list_messages(conn, svc["id"])
        security_rules = flows_repo.list_static_security_requirements_in_declaration_order(conn, svc["id"])
        cloud_facts = flows_repo.list_static_cloud_facts(conn, svc["id"])

        lines = [
            f"# {svc['name']}",
            "",
            service_row["short_desc"] or "",
            "",
            service_row["long_desc"] or "",
            "",
            f"**Stack:** {svc['stack'] or '?'}",
            "",
            "## Depends on",
            *(_fmt_calls(calls) if calls else []),
            *(f"- **{target}** (http (unresolved), declared target)" for target in declared_targets),
            *(["- (no dependency detected)"] if not calls and not declared_targets else []),
            "",
            "## APIs",
        ]
        if apis:
            for a in apis:
                slug = _slug(f"{a['method']}-{a['path']}")
                lines.append(f"- `{a['method']} {a['path']}` — {a['summary'] or ''} ([detail](apis/{slug}.md))")
        else:
            lines.append("- (no API detected)")

        persistence_lines = [f"- **{p['name']}** ({p['kind']})" for p in persistence]
        if has_unrepresented_mongo_access(snapshot, (p["engine"] for p in persistence)):
            persistence_lines.append("- **MongoDB** (accesses; collection unresolved from static analysis)")
        lines += ["", "## Persistence", *(persistence_lines or ["- (none detected)"])]

        publishes = [m for m in messages if m["direction"] == "publishes"]
        consumes = [m for m in messages if m["direction"] == "consumes"]
        publish_lines = _fmt_messages(publishes) if publishes else []
        if has_confirmed_redis_publication(snapshot):
            publish_lines.append("- **Redis Pub/Sub** (publishes; channel unresolved from static analysis)")
        lines += [
            "", "## Messaging", "", "### Publishes",
            *(publish_lines or ["- (none detected)"]),
            "", "### Consumes", *_fmt_messages(consumes),
        ]

        lines += ["", "## Cloud"]
        if cloud_facts:
            lines += [
                f"- **{f['provider']}:{f['service_name']}** {f['operation']} "
                f"({f['target_name'] or 'unresolved target'})"
                for f in cloud_facts
            ]
        else:
            lines.append("- (none detected)")

        index_path = service_dir / "index.md"
        index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(index_path)

        if apis:
            apis_dir = service_dir / "apis"
            apis_dir.mkdir(parents=True, exist_ok=True)
            for a in apis:
                api_row = apis_repo.get_api_by_key(conn, svc["id"], a["method"], a["path"])
                api_calls = service_calls_repo.list_calls_for_api(conn, api_row["id"])
                validations = apis_repo.list_validations_for_api(conn, api_row["id"])
                response_shape = json.loads(api_row["response_shape"] or "[]")
                headers = flows_repo.list_static_api_headers_for_route(conn, svc["id"], a["method"], a["path"])
                source_calls = route_declared_http_calls(
                    navigator, a["method"], a["path"],
                    (call["to_service_name"] for call in api_calls),
                )

                api_lines = [f"# {a['method']} {a['path']}", "", api_row["description"] or "", "", "## Response"]
                api_lines += [f"- `{f['field']}`: {f['type_desc']}" for f in response_shape] or ["(not detected)"]
                for direction in ("request", "response"):
                    names = [header["name"] for header in headers if header["direction"] == direction]
                    if names:
                        api_lines += ["", f"## {direction.title()} headers", *(f"- `{name}`" for name in names)]
                call_lines = _fmt_calls(api_calls) if api_calls else []
                for call in source_calls.calls:
                    operation = " ".join(part for part in (call.method, call.path) if part)
                    suffix = f": {operation}" if operation else ""
                    call_lines.append(f"- **{call.target_service}** (http (unresolved), source-proven){suffix}")
                if source_calls.status is RouteCallStatus.LIMITED:
                    call_lines.append("- (static flow limited; other calls may exist)")
                elif source_calls.status is RouteCallStatus.UNASSESSED:
                    call_lines.append("- (route source flow unavailable; additional calls unknown)")
                api_lines += ["", "## Calls", *(call_lines or ["- (no dependency detected)"])]
                security = flows_repo.matching_route_security_requirement(
                    security_rules, a["method"], a["path"],
                )
                if security is not None:
                    roles = json.loads(security["roles_json"])
                    role_suffix = f" (roles: {', '.join(roles)})" if roles else ""
                    api_lines += ["", "## Declared route security", f"- {security['requirement']}{role_suffix}"]
                api_lines += ["", "## Validations / Constraints"]
                if validations:
                    api_lines += [f"- [{v['kind']}] {v['description']}" for v in validations]
                else:
                    api_lines.append("(none detected)")

                slug = _slug(f"{a['method']}-{a['path']}")
                api_path = apis_dir / f"{slug}.md"
                api_path.write_text("\n".join(api_lines) + "\n", encoding="utf-8")
                written.append(api_path)

    return written
