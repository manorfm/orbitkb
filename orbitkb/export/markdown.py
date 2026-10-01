from __future__ import annotations

import hashlib
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
from orbitkb.domain.route_calls import (
    DeclaredHttpCall,
    RouteCallStatus,
    route_declared_http_calls,
)
from orbitkb.export.dependencies import unresolved_declared_http_targets
from orbitkb.export.messaging import (
    has_confirmed_redis_publication,
    messaging_analysis_status,
)
from orbitkb.export.page_manifest import (
    MANIFEST_NAME,
    owned_pages,
    page_digest,
    save_owned_pages,
)
from orbitkb.export.paths import service_output_dirs
from orbitkb.export.persistence import has_unrepresented_mongo_access


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower() or "root"


def _api_page_names(apis: list[sqlite3.Row], unavailable: set[str] | None = None) -> dict[tuple[str, str], str]:
    """Give each route one stable filename, disambiguating normalized collisions."""
    used: set[str] = set(unavailable or ())
    names: dict[tuple[str, str], str] = {}
    for api in apis:
        key = (api["method"], api["path"])
        base = _slug(f"{key[0]}-{key[1]}")
        name = base
        if f"{name}.md".casefold() in used:
            digest = hashlib.sha256(f"{key[0]}\0{key[1]}".encode("utf-8")).hexdigest()[:10]
            name = f"{base}--{digest}"
            suffix = 2
            while f"{name}.md".casefold() in used:
                name = f"{base}--{digest}-{suffix}"
                suffix += 1
        used.add(f"{name}.md".casefold())
        names[key] = f"{name}.md"
    return names


def _fmt_calls(calls: list[sqlite3.Row]) -> list[str]:
    lines = []
    for c in calls:
        data_needed = ", ".join(json.loads(c["data_needed"] or "[]"))
        call_kind = c["call_kind"] + (" (unresolved)" if c["target_kind"] == "unknown" else "")
        line = f"- **{c['to_service_name']}** ({call_kind}, {c['purpose_kind']}): {c['reason']}"
        if data_needed:
            line += f" — needs: {data_needed}"
        lines.append(line)
    return lines


def _fmt_api_calls(calls: list[sqlite3.Row], source_calls: tuple[DeclaredHttpCall, ...]) -> list[str]:
    lines = _fmt_calls(calls)
    represented: set[str] = set()
    for index, call in enumerate(calls):
        target = call["to_service_name"]
        if call["call_kind"] != "http" or target in represented:
            continue
        operations = [" ".join(part for part in (source.method, source.path) if part)
                      for source in source_calls if source.target_service == target]
        if operations:
            lines[index] += f" — source-proven operations: {'; '.join(operations)}"
            represented.add(target)
    for source in source_calls:
        if source.target_service in represented:
            continue
        operation = " ".join(part for part in (source.method, source.path) if part)
        suffix = f": {operation}" if operation else ""
        lines.append(f"- **{source.target_service}** (http (unresolved), source-proven){suffix}")
    return lines


def _fmt_messages(messages: list[sqlite3.Row]) -> list[str]:
    return [f"- **{m['channel']}** ({m['direction']}): {m['description'] or ''}" for m in messages]


def export_markdown(conn: sqlite3.Connection, out_dir: Path, service_filter: str | None = None) -> list[Path]:
    written: list[Path] = []
    services = services_repo.list_services(conn)
    output_dirs = service_output_dirs(out_dir, services)
    for svc in services:
        if service_filter and svc["name"] != service_filter:
            continue
        service_row = services_repo.get_service_by_id(conn, svc["id"])
        service_dir = output_dirs[svc["id"]]
        if service_dir.is_symlink():
            raise ValueError(f"service export directory must not be a symlink: {service_dir}")
        service_dir.mkdir(parents=True, exist_ok=True)

        calls = service_calls_repo.list_calls_for_service(conn, svc["id"])
        declared_targets = unresolved_declared_http_targets(
            flows_repo.list_static_service_calls(conn, svc["id"]),
            (call["to_service_name"] for call in calls),
        )
        apis = apis_repo.list_apis(conn, svc["id"])
        apis_dir = service_dir / "apis"
        if apis_dir.is_symlink():
            raise ValueError(f"API export directory must not be a symlink: {apis_dir}")
        if apis or apis_dir.exists():
            apis_dir.mkdir(parents=True, exist_ok=True)
            previous_pages = owned_pages(apis_dir)
            unavailable = {
                path.name.casefold() for path in apis_dir.iterdir()
                if path.name not in previous_pages and path.name != MANIFEST_NAME
            }
        else:
            previous_pages = {}
            unavailable = set()
        api_page_names = _api_page_names(apis, unavailable)
        persistence = persistence_repo.list_persistence(conn, svc["id"])
        snapshot = snapshots_repo.read_snapshot(conn, svc["id"])
        navigator = KnowledgeNavigator(snapshot) if snapshot is not None else None
        messages = messages_repo.list_messages(conn, svc["id"])
        security_rules = flows_repo.list_static_security_requirements_in_declaration_order(conn, svc["id"])
        cloud_facts = flows_repo.list_static_cloud_facts(conn, svc["id"])
        empty_dependencies = (
            "- (dependency analysis unavailable)" if snapshot is None else "- (no dependency detected)"
        )

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
            *_fmt_calls(calls),
            *(f"- **{target}** (http (unresolved), declared target)" for target in declared_targets),
            *([empty_dependencies] if not calls and not declared_targets else []),
            "",
            "## APIs",
        ]
        if apis:
            for a in apis:
                page_name = api_page_names[(a["method"], a["path"])]
                lines.append(f"- `{a['method']} {a['path']}` — {a['summary'] or ''} ([detail](apis/{page_name}))")
        else:
            lines.append("- (no API detected)")

        persistence_lines = [f"- **{p['name']}** ({p['kind']})" for p in persistence]
        if has_unrepresented_mongo_access(snapshot, (p["engine"] for p in persistence)):
            persistence_lines.append("- **MongoDB** (accesses; collection unresolved from static analysis)")
        lines += ["", "## Persistence", *(persistence_lines or ["- (none detected)"])]

        publishes = [m for m in messages if m["direction"] == "publishes"]
        consumes = [m for m in messages if m["direction"] == "consumes"]
        status = messaging_analysis_status(snapshot)
        empty_message = "- (none detected)" if status == "supported" else "- (not assessed)"
        publish_lines = _fmt_messages(publishes)
        if has_confirmed_redis_publication(snapshot):
            publish_lines.append("- **Redis Pub/Sub** (publishes; channel unresolved from static analysis)")
        lines += [
            "", "## Messaging", "", f"**Static analysis:** {status}", "", "### Publishes",
            *(publish_lines or [empty_message]),
            "", "### Consumes", *(_fmt_messages(consumes) or [empty_message]),
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

        current_pages: dict[str, str] = {}
        if apis:
            for a in apis:
                api_row = apis_repo.get_api_by_key(conn, svc["id"], a["method"], a["path"])
                api_calls = service_calls_repo.list_calls_for_api(conn, api_row["id"])
                validations = apis_repo.list_validations_for_api(conn, api_row["id"])
                response_shape = json.loads(api_row["response_shape"] or "[]")
                headers = flows_repo.list_static_api_headers_for_route(conn, svc["id"], a["method"], a["path"])
                source_calls = route_declared_http_calls(
                    navigator, a["method"], a["path"],
                )

                api_lines = [f"# {a['method']} {a['path']}", "", api_row["description"] or "", "", "## Response"]
                api_lines += [f"- `{f['field']}`: {f['type_desc']}" for f in response_shape] or ["(not detected)"]
                for direction in ("request", "response"):
                    names = [header["name"] for header in headers if header["direction"] == direction]
                    if names:
                        api_lines += ["", f"## {direction.title()} headers", *(f"- `{name}`" for name in names)]
                call_lines = _fmt_api_calls(api_calls, source_calls.calls)
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

                api_path = apis_dir / api_page_names[(a["method"], a["path"])]
                data = ("\n".join(api_lines) + "\n").encode("utf-8")
                api_path.write_bytes(data)
                current_pages[api_path.name] = page_digest(data)
                written.append(api_path)
        if apis_dir.exists():
            save_owned_pages(apis_dir, previous_pages, current_pages)

    return written
