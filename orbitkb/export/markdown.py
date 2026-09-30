from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.export.dependencies import unresolved_declared_http_targets


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower() or "root"


def _fmt_calls(calls: list[sqlite3.Row]) -> list[str]:
    lines = []
    for c in calls:
        data_needed = ", ".join(json.loads(c["data_needed"] or "[]"))
        line = f"- **{c['to_service_name']}** ({c['call_kind']}, {c['purpose_kind']}): {c['reason']}"
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
        messages = messages_repo.list_messages(conn, svc["id"])
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

        lines += ["", "## Persistence"]
        lines += [f"- **{p['name']}** ({p['kind']})" for p in persistence] or ["- (none detected)"]

        publishes = [m for m in messages if m["direction"] == "publishes"]
        consumes = [m for m in messages if m["direction"] == "consumes"]
        lines += ["", "## Messaging", "", "### Publishes", *_fmt_messages(publishes), "", "### Consumes", *_fmt_messages(consumes)]

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

                api_lines = [f"# {a['method']} {a['path']}", "", api_row["description"] or "", "", "## Response"]
                api_lines += [f"- `{f['field']}`: {f['type_desc']}" for f in response_shape] or ["(not detected)"]
                api_lines += ["", "## Calls", *_fmt_calls(api_calls)]
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
