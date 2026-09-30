from __future__ import annotations

import argparse
import faulthandler
import gc
import json
import logging
import math
import os
import sqlite3
import sys
import textwrap
import time
from pathlib import Path

import orbitkb
from orbitkb.analysis.depth import DepthMode, resolve_depth_provider
from orbitkb.cli_progress import (
    CompositeProgressReporter,
    LocalIndexProgressReporter,
    RichProgressReporter,
)
from orbitkb.config import resolve_backend
from orbitkb.db.backup import backup_database, restore_database
from orbitkb.db.connection import DEFAULT_DB_PATH, open_db, open_readonly_db
from orbitkb.db.repositories import index_runs as index_runs_repo
from orbitkb.db.repositories import indexed_files as indexed_files_repo
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.db.repositories import search as search_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.db.repositories import verification as verification_repo
from orbitkb.export.markdown import export_markdown
from orbitkb.export.mermaid import export_mermaid
from orbitkb.generation.architecture import recompute_architecture_view
from orbitkb.generation.backend_base import GenerationError
from orbitkb.generation.embeddings import try_create_default_backend
from orbitkb.generation.orchestrator import DiscoveryError, index_path, index_service
from orbitkb.generation.verification import verify_change_surface
from orbitkb.mcp import queries as mcp_queries
from orbitkb.monitor import (
    collect_snapshot,
    render_snapshot,
    should_render_snapshot,
    snapshot_state_key,
)
from orbitkb.setup.actions import SetupAction

_METRICS_INTERVAL_ENV = "ORBITKB_METRICS_INTERVAL"
_METRICS_MIN_INTERVAL_ENV = "ORBITKB_METRICS_MIN_INTERVAL"


def _configure_verbose_logging(verbose: bool) -> None:
    """`--verbose` names the exact file and function being analyzed at DEBUG
    level (see analysis/engine.py). A native crash (a segfault, not a
    catchable Python exception) leaves no traceback to inspect afterward --
    the last flushed line here is what actually pinpoints it, without needing
    the source file itself."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(message)s", stream=sys.stderr, force=True)


def _cmd_index(args: argparse.Namespace) -> int:
    _configure_verbose_logging(args.verbose)
    conn = open_db(args.db)
    backend = resolve_backend(args.backend, args.model, args.claude_bare, args.codex_api_key)
    embedding_backend = try_create_default_backend()
    try:
        depth_provider = resolve_depth_provider(
            DepthMode(args.depth_mode), args.depth_command, tuple(args.depth_arg), args.depth_tool,
            args.depth_timeout, args.depth_max_edges, args.depth_cache_entries,
            args.depth_circuit_failures, args.depth_circuit_cooldown,
        )
        with RichProgressReporter() as terminal_progress:
            progress = CompositeProgressReporter(terminal_progress, LocalIndexProgressReporter(args.db))
            results = index_path(
                conn, Path(args.path), backend, service_override=args.service, force=args.force,
                progress=progress, repository_name=args.repository_name, embedding_backend=embedding_backend,
                stack_override=args.stack, depth_provider=depth_provider,
            )
    except (DiscoveryError, GenerationError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for r in results:
        print(
            f"{r.service_name}: status={r.status} files_changed={r.files_changed} llm_calls={r.llm_calls} "
            f"llm_invocations={r.llm_invocations} "
            f"cost_usd={r.cost_usd}"
        )
        if args.sufficiency_details:
            _print_sufficiency_details(r)
    if args.depth_mode != DepthMode.OFF.value:
        print(f"depth_provider_metrics={json.dumps(depth_provider.metrics(), sort_keys=True)}")
    return 0 if all(r.status == "ok" for r in results) else 1


def _update_one_service(
    conn, row, args: argparse.Namespace, backend, embedding_backend, depth_provider, progress,
) -> bool:
    """Re-index one already-known service row. Prints one status/error line. Returns success."""
    root = Path(row["root_path"])
    if not root.is_dir():
        print(
            f"error: root path for {row['name']!r} no longer exists: {root}\n"
            f"       re-run `orbitkb index <newpath> --service {row['name']}` instead.",
            file=sys.stderr,
        )
        return False
    from orbitkb.discovery.registry import detector_for

    detector = detector_for(root)
    if detector is None:
        print(f"error: {root} no longer matches any known stack", file=sys.stderr)
        return False
    try:
        result = index_service(
            conn, row["name"], root, detector, backend, force=args.force, progress=progress,
            embedding_backend=embedding_backend, depth_provider=depth_provider,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False
    print(
        f"{result.service_name}: status={result.status} files_changed={result.files_changed} "
        f"llm_calls={result.llm_calls} llm_invocations={result.llm_invocations} cost_usd={result.cost_usd}"
    )
    if args.sufficiency_details:
        _print_sufficiency_details(result)
    return result.status == "ok"


def _print_sufficiency_details(result) -> None:
    for detail in result.sufficiency_details:
        assessment = detail.assessment
        payload = {
            "service": result.service_name,
            "method": detail.method,
            "path": detail.path,
            "status": detail.status,
            "render_status": detail.render_status,
            "dimensions": [
                {
                    "dimension": item.dimension,
                    "status": item.status.value,
                    "reason": item.reason,
                    "evidence_ids": item.evidence_ids,
                }
                for item in assessment.dimensions
            ] if assessment else [],
            "omitted_fact_ids": assessment.omitted_fact_ids if assessment else (),
            "reason": None if assessment else "matching canonical endpoint was not found",
        }
        print(f"sufficiency={json.dumps(payload, sort_keys=True)}")


def _cmd_update(args: argparse.Namespace) -> int:
    _configure_verbose_logging(args.verbose)
    if args.service and args.repository:
        print("error: pass either a service name or --repository, not both", file=sys.stderr)
        return 1
    if not args.service and not args.repository:
        print("error: pass a service name or --repository", file=sys.stderr)
        return 1
    conn = open_db(args.db)
    if args.repository:
        repo = repositories_repo.get_repository_by_name(conn, args.repository)
        if repo is None:
            print(f"error: unknown repository {args.repository!r} (run `orbitkb list`)", file=sys.stderr)
            return 1
        rows = services_repo.list_services_for_repository(conn, repo["id"])
        if not rows:
            print(f"error: repository {args.repository!r} has no indexed services", file=sys.stderr)
            return 1
    else:
        row = services_repo.get_service_by_name(conn, args.service)
        if row is None:
            print(f"error: unknown service {args.service!r} (run `orbitkb list`)", file=sys.stderr)
            return 1
        rows = [row]

    backend = resolve_backend(args.backend, args.model, args.claude_bare, args.codex_api_key)
    embedding_backend = try_create_default_backend()
    try:
        depth_provider = resolve_depth_provider(
            DepthMode(args.depth_mode), args.depth_command, tuple(args.depth_arg), args.depth_tool,
            args.depth_timeout, args.depth_max_edges, args.depth_cache_entries,
            args.depth_circuit_failures, args.depth_circuit_cooldown,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    all_ok = True
    with RichProgressReporter() as terminal_progress:
        progress = CompositeProgressReporter(terminal_progress, LocalIndexProgressReporter(args.db))
        for row in rows:
            all_ok = _update_one_service(conn, row, args, backend, embedding_backend, depth_provider, progress) and all_ok
    if args.depth_mode != DepthMode.OFF.value:
        print(f"depth_provider_metrics={json.dumps(depth_provider.metrics(), sort_keys=True)}")
    return 0 if all_ok else 1


def _cmd_list(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    rows = services_repo.list_services(conn)
    if not rows:
        print("(nenhum serviço indexado ainda)")
        return 0
    for r in rows:
        repository = r["repository_name"] or "(standalone)"
        print(
            f"{r['name']:<30} repository={repository:<20} stack={r['stack'] or '?':<14} "
            f"apis={r['api_count']:<3} {r['short_desc'] or ''}"
        )
    return 0


_SETUP_CLIENTS = ("claude", "cursor", "codex")


def _print_setup_summary(actions: list[SetupAction]) -> None:
    needs_attention = [a for a in actions if a.status in ("conflict", "declined")]
    for action in [a for a in actions if a.status not in ("conflict", "declined")]:
        print(f"[{action.category}] {action.client} ({action.scope or 'n/a'}): {action.status} — {action.path}")
    for action in needs_attention:
        print(f"[{action.category}] {action.client} ({action.scope or 'n/a'}): {action.status.upper()} — {action.path}")
        if action.detail:
            print(action.detail)


def _resolve_setup_scope_and_repository_root(args: argparse.Namespace) -> tuple[str, Path | None, int | None]:
    """Shared by the write and remove paths of `orbitkb setup`. Returns
    (scope, repository_root, error_exit_code); error_exit_code is None on
    success (an error message was already printed to stderr otherwise)."""
    scope = args.scope
    if scope is None:
        scope = "project" if args.repository else "user"
    if scope == "project" and not args.repository:
        print("error: --scope project requires --repository", file=sys.stderr)
        return scope, None, 1

    if not args.repository:
        return scope, None, None
    conn = open_db(args.db)
    repo = repositories_repo.get_repository_by_name(conn, args.repository)
    if repo is None:
        print(f"error: unknown repository {args.repository!r} (run `orbitkb list`)", file=sys.stderr)
        return scope, None, 1
    return scope, Path(repo["root_path"]), None


def _running_interactively() -> bool:
    """Whether it's safe to block on a prompt: a real terminal on both ends.
    False in CI, in a script, or when an agent shells out to this command —
    the exact case `orbitkb setup` must never hang waiting for input."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _resolve_setup_clients(args: argparse.Namespace) -> list[str]:
    if args.client:
        return args.client
    from orbitkb.setup.client_detection import (
        choose_clients_interactively,
        detect_available_clients,
    )

    candidates = detect_available_clients() or list(_SETUP_CLIENTS)
    if not args.yes and _running_interactively():
        return choose_clients_interactively(candidates)
    return candidates


def _cmd_setup(args: argparse.Namespace) -> int:
    if args.remove:
        return _cmd_setup_remove(args)

    from orbitkb.setup.agent_instructions import write_agent_instructions
    from orbitkb.setup.git_hook import install_repository_hooks
    from orbitkb.setup.mcp_config import (
        mcp_command_line,
        write_claude_code_config,
        write_codex_config,
        write_cursor_config,
    )

    scope, repository_root, error_exit_code = _resolve_setup_scope_and_repository_root(args)
    if error_exit_code is not None:
        return error_exit_code

    clients = _resolve_setup_clients(args)
    command, mcp_args = mcp_command_line(args.backend, args.model, args.claude_bare, args.codex_api_key, args.db)
    target_root = repository_root or Path.cwd()

    actions: list[SetupAction] = []
    if "claude" in clients:
        actions.append(write_claude_code_config(target_root, scope, command, mcp_args, force=args.force, dry_run=args.dry_run))
    if "cursor" in clients:
        actions.append(write_cursor_config(target_root, scope, command, mcp_args, force=args.force, dry_run=args.dry_run))
    if "codex" in clients:
        actions.append(write_codex_config(command, mcp_args, force=args.force, dry_run=args.dry_run))

    if repository_root is not None:
        actions.extend(install_repository_hooks(repository_root, args.repository, args.db, dry_run=args.dry_run))
        actions.extend(write_agent_instructions(repository_root, args.repository, args.db, dry_run=args.dry_run))

    _print_setup_summary(actions)
    return 1 if any(a.status == "conflict" for a in actions) else 0


def _cmd_setup_remove(args: argparse.Namespace) -> int:
    from orbitkb.setup.agent_instructions import remove_agent_instructions
    from orbitkb.setup.git_hook import uninstall_repository_hooks
    from orbitkb.setup.mcp_config import (
        remove_claude_code_config,
        remove_codex_config,
        remove_cursor_config,
    )

    scope, repository_root, error_exit_code = _resolve_setup_scope_and_repository_root(args)
    if error_exit_code is not None:
        return error_exit_code

    clients = args.client or list(_SETUP_CLIENTS)
    target_root = repository_root or Path.cwd()
    dry_run = not args.yes

    actions: list[SetupAction] = []
    if "claude" in clients:
        actions.append(remove_claude_code_config(target_root, scope, dry_run=dry_run))
    if "cursor" in clients:
        actions.append(remove_cursor_config(target_root, scope, dry_run=dry_run))
    if "codex" in clients:
        actions.append(remove_codex_config(dry_run=dry_run))
    if repository_root is not None:
        actions.extend(uninstall_repository_hooks(repository_root, dry_run=dry_run))
        actions.extend(remove_agent_instructions(repository_root, dry_run=dry_run))

    _print_setup_summary(actions)
    if not args.yes:
        print("\n(preview only — re-run with --yes to actually remove)")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    if args.units and not args.service:
        print("error: --units requires a service name", file=sys.stderr)
        return 1
    conn = open_db(args.db)
    if args.service:
        row = services_repo.get_service_by_name(conn, args.service)
        if row is None:
            print(f"error: unknown service {args.service!r}", file=sys.stderr)
            return 1
        hashes = indexed_files_repo.get_indexed_file_hashes(conn, row["id"])
        print(f"service: {row['name']} ({row['stack']}) — {row['root_path']}")
        print(f"last_commit: {row['last_commit']}")
        print(f"indexed files: {len(hashes)}")
        for run in index_runs_repo.recent_index_runs(conn, row["id"], limit=5):
            print(
                f"  run#{run['id']} {run['started_at']} status={run['status']} backend={run['backend']} "
                f"files_changed={run['files_changed']} llm_calls={run['llm_calls']} "
                f"llm_invocations={run['llm_invocations']} "
                f"tokens=(in={run['input_tokens']},out={run['output_tokens']}) cost_usd={run['cost_usd']} "
                f"notes={run['notes']}"
            )
            for unit in index_runs_repo.list_unit_usage(conn, run["id"]):
                print(
                    f"    {unit['unit_kind']}: generated={unit['generated_units']} "
                    f"invocations={unit['llm_invocations']} failed={bool(unit['had_failure'])} "
                    f"tokens=(in={unit['input_tokens']},out={unit['output_tokens']}) "
                    f"cost_usd={unit['cost_usd']} backend_duration_ms={unit['backend_duration_ms']}"
                )
            if args.units:
                for unit in index_runs_repo.list_run_units(conn, run["id"]):
                    print(
                        f"      unit {unit['unit_kind']} key={unit['unit_key']} "
                        f"status={unit['status']} attempts={unit['llm_invocations']} "
                        f"tokens=(in={unit['input_tokens']},out={unit['output_tokens']},"
                        f"cached={unit['cached_input_tokens']}) "
                        f"cost_usd={unit['cost_usd']} backend_duration_ms={unit['backend_duration_ms']} "
                        f"prompt_chars={unit['prompt_chars']}"
                    )
        totals = index_runs_repo.usage_totals(conn, row["id"])
        print(
            f"cumulative usage: input_tokens={totals['input_tokens']} output_tokens={totals['output_tokens']} "
            f"cost_usd={totals['cost_usd']}"
        )
    else:
        services = services_repo.list_services(conn)
        print(f"services indexed: {len(services)}")
        for run in index_runs_repo.recent_index_runs(conn, limit=10):
            svc = services_repo.get_service_by_id(conn, run["service_id"]) if run["service_id"] else None
            name = svc["name"] if svc else "?"
            print(
                f"  run#{run['id']} service={name} status={run['status']} backend={run['backend']} "
                f"files_changed={run['files_changed']} llm_calls={run['llm_calls']} "
                f"llm_invocations={run['llm_invocations']} cost_usd={run['cost_usd']}"
            )
        totals = index_runs_repo.usage_totals(conn)
        print(
            f"cumulative usage: input_tokens={totals['input_tokens']} output_tokens={totals['output_tokens']} "
            f"cost_usd={totals['cost_usd']}"
        )
        verifications = verification_repo.latest_verifications(conn, limit=5)
        if verifications:
            print("recent change surface verifications:")
            for v in verifications:
                print(
                    f"  run#{v['run_id']} repository={v['repository']} since={v['since_commit']} "
                    f"precision={v['precision']} recall={v['recall']}"
                )
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    service_count = repositories_repo.delete_repository(conn, args.repository)
    if service_count is None:
        print(f"error: unknown repository {args.repository!r}", file=sys.stderr)
        return 1
    service_calls_repo.reconcile_service_call_targets(conn)
    search_repo.rebuild_search_index(conn)
    recompute_architecture_view(conn)
    print(f"removed repository {args.repository} and {service_count} service(s)")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    if args.format == "mermaid":
        written = export_mermaid(conn, Path(args.out), service_filter=args.service)
    else:
        written = export_markdown(conn, Path(args.out), service_filter=args.service)
    print(f"wrote {len(written)} files under {args.out}")
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    backup_database(args.db, args.out)
    print(f"backed up {args.db} to {args.out}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    try:
        restore_database(args.source, args.db)
    except FileNotFoundError:
        print(f"error: backup not found: {args.source}", file=sys.stderr)
        return 1
    print(f"restored {args.db} from {args.source}")
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    backend = resolve_backend(args.backend, args.model, args.claude_bare, args.codex_api_key)
    result = mcp_queries.find_change_surface(
        conn, backend, args.task, hint_services=args.hint_services, repository=args.repository,
    )
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    backend = resolve_backend(args.backend, args.model, args.claude_bare, args.codex_api_key)
    result = mcp_queries.get_change_context(
        conn, backend, args.task, hint_services=args.hint_services,
        repository=args.repository, max_services=args.max_services,
        epic_type=args.epic_type,
    )
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_context_feedback(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    result = mcp_queries.record_change_context_feedback(
        conn, args.run_id, args.outcome, args.note, args.missing_services,
    )
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_context_query(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    result = mcp_queries.record_context_query_execution(conn, args.run_id, args.tool, args.service)
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_context_metrics(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    result = mcp_queries.get_context_budget_metrics(conn, args.epic_type)
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_metrics(args: argparse.Namespace) -> int:
    """Display local, redacted operational metrics without writing to SQLite."""
    if args.watch:
        try:
            interval, interval_source, floor_active = _resolve_metrics_watch_interval(args.interval)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    try:
        conn = open_readonly_db(args.db)
    except sqlite3.Error as exc:
        print(f"error: cannot open local metrics database: {exc}", file=sys.stderr)
        return 1
    try:
        color = not args.no_color and sys.stdout.isatty()
        previous_state = None
        while True:
            snapshot = collect_snapshot(conn)
            if should_render_snapshot(snapshot, previous_state, alerts_only=args.alerts_only):
                if args.watch and sys.stdout.isatty():
                    print("\033[2J\033[H", end="")
                print(render_snapshot(
                    snapshot,
                    color=color,
                    alerts_only=args.alerts_only,
                    include_updated_at=args.alerts_only,
                    watch_interval=interval if args.watch and not args.alerts_only else None,
                    watch_interval_source=interval_source if args.watch and not args.alerts_only else None,
                    watch_interval_floor_active=floor_active if args.watch and not args.alerts_only else False,
                ))
                previous_state = snapshot_state_key(snapshot, alerts_only=args.alerts_only)
            if not args.watch:
                return 0
            time.sleep(interval)
    finally:
        conn.close()


def _resolve_metrics_watch_interval(command_interval: float | None) -> tuple[float, str, bool]:
    environment_value = os.environ.get(_METRICS_INTERVAL_ENV)
    if command_interval is not None:
        value, source = command_interval, "CLI"
    elif environment_value is not None:
        value, source = environment_value, "environment"
    else:
        value, source = "1.0", "default"
    try:
        interval = float(value)
    except (TypeError, ValueError) as exc:
        source = "--interval" if command_interval is not None else _METRICS_INTERVAL_ENV
        raise ValueError(f"{source} must be a number") from exc
    if not math.isfinite(interval):
        raise ValueError("interval must be finite in --watch mode")
    if interval <= 0:
        raise ValueError("interval must be greater than zero in --watch mode")
    minimum = _resolve_metrics_min_interval()
    if minimum is not None and interval < minimum:
        raise ValueError(f"interval must be at least {minimum:g} seconds in --watch mode")
    return interval, source, minimum is not None


def _resolve_metrics_min_interval() -> float | None:
    value = os.environ.get(_METRICS_MIN_INTERVAL_ENV)
    if value is None:
        return None
    try:
        minimum = float(value)
    except ValueError as exc:
        raise ValueError(f"{_METRICS_MIN_INTERVAL_ENV} must be a number") from exc
    if not math.isfinite(minimum):
        raise ValueError(f"{_METRICS_MIN_INTERVAL_ENV} must be finite")
    if minimum <= 0:
        raise ValueError(f"{_METRICS_MIN_INTERVAL_ENV} must be greater than zero")
    return minimum


def _cmd_context_verify(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    result = mcp_queries.verify_context_budget(conn, args.run_id, args.repository, args.since)
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_runtime_ingest(args: argparse.Namespace) -> int:
    try:
        observations = json.loads(args.observations)
    except json.JSONDecodeError:
        print("error: observations must be a JSON array", file=sys.stderr)
        return 1
    conn = open_db(args.db)
    result = mcp_queries.ingest_runtime_evidence(conn, args.service, args.source, observations, args.repository)
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


def _cmd_runtime_divergence(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    result = mcp_queries.describe_runtime_divergence(conn, args.service, args.repository)
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


def _cmd_verify(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    result = verify_change_surface(conn, args.run_id, args.repository, args.since, record_feedback=args.record_feedback)
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    print(f"predicted: {result['predicted']}")
    print(f"actual:    {result['actual']}")
    print(f"true_positives:  {result['true_positives']}")
    print(f"false_positives: {result['false_positives']}")
    print(f"false_negatives: {result['false_negatives']}")
    print(f"precision: {result['precision']}")
    print(f"recall:    {result['recall']}")
    if args.record_feedback:
        print("feedback recorded for true/false positives")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from orbitkb.mcp.server import build_server

    backend = resolve_backend(args.backend, args.model, args.claude_bare, args.codex_api_key)
    server = build_server(args.db, backend=backend)
    server.run()
    return 0


_TOP_LEVEL_EPILOG = """\
The orbitkb workflow is index -> ask -> verify:

  1. index   Point it at a repo (or several) so it builds a System Knowledge Model.
  2. ask     Register it as an MCP server (`serve`) and have an agent call
             find_change_surface with an engineering task/epic, before it opens
             any file, to get the likely blast radius with evidence + confidence.
  3. verify  Once the change ships, check whether the prediction was right
             against the real git diff, closing the feedback loop.

Any time after indexing, `export` turns the same knowledge into static docs
(Markdown) or diagrams (Mermaid) for humans reading outside an MCP client —
no LLM call, no extra cost.

Examples:
  orbitkb index ~/code/my-monorepo --repository-name my-monorepo
  orbitkb remove --repository retired-monorepo
  orbitkb export md --out docs/
  orbitkb serve --backend claude
  orbitkb verify 3 --repository my-monorepo --since a1b2c3d

Run `orbitkb <command> --help` for a runnable example of any single command.
"""


def build_parser() -> argparse.ArgumentParser:
    # No `epilog=`/RawDescriptionHelpFormatter here: the top-level `-h`/`--help`/bare
    # invocation is rendered by _render_top_level_help (main() intercepts it before
    # parse_args ever runs), which folds _TOP_LEVEL_EPILOG in directly. Only each
    # subcommand's own parser (below) still uses argparse's own help rendering.
    parser = argparse.ArgumentParser(prog="orbitkb")
    parser.add_argument("--version", action="version", version=f"orbitkb {orbitkb.__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def add_backend_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--backend", choices=["claude", "codex", "mock"], default=None,
            help="LLM backend to shell out to headless (default: whichever CLI is on PATH); "
            "'mock' costs nothing and shells out to nothing, for dry runs against a real repository",
        )
        p.add_argument("--model", default=None, help="Override the backend's default model")
        p.add_argument("--claude-bare", action="store_true", help="Use ANTHROPIC_API_KEY billing instead of the Claude CLI subscription session")
        p.add_argument("--codex-api-key", action="store_true", help="Use CODEX_API_KEY billing instead of the ChatGPT subscription session")
        p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")

    def add_depth_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--depth-mode", choices=[mode.value for mode in DepthMode], default=DepthMode.OFF.value,
            help="Flow depth: off (static analysis only), augment (optional MCP enrichment), or require.",
        )
        p.add_argument("--depth-command", default=None, help="External code-intelligence MCP executable for augment/require mode.")
        p.add_argument("--depth-arg", action="append", default=[], help="One argument for --depth-command; repeat for multiple arguments.")
        p.add_argument("--depth-tool", default="trace_entrypoint", help="MCP tool returning the documented bounded edge payload.")
        p.add_argument("--depth-timeout", type=float, default=15.0, help="Maximum seconds for the external MCP session (default: 15).")
        p.add_argument("--depth-max-edges", type=int, default=100, help="Maximum enriched edges per indexed service (default: 100).")
        p.add_argument("--depth-cache-entries", type=int, default=128, help="Maximum bounded enrichment results cached per index process (default: 128).")
        p.add_argument("--depth-circuit-failures", type=int, default=3, help="Failures before optional enrichment opens its circuit (default: 3).")
        p.add_argument("--depth-circuit-cooldown", type=float, default=30.0, help="Seconds before an open enrichment circuit may retry (default: 30).")

    p_index = sub.add_parser(
        "index", help="Index a monorepo root or a single service repo",
        epilog=(
            "examples:\n"
            "  orbitkb index ~/code/orders-service\n"
            "  orbitkb index ~/code/my-monorepo --repository-name my-monorepo\n"
            "  orbitkb index . --service custom-name --force\n"
            "  # --stack bypasses auto-detection for a folder no heuristic recognizes\n"
            "  # (e.g. a library/CLI package, not a web microservice):\n"
            "  orbitkb index . --service orbitkb-core --stack python\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_index.add_argument("path", help="A single service's root, or a monorepo root containing several")
    p_index.add_argument("--service", default=None, help="Override the inferred service name (only valid for a single-service path)")
    p_index.add_argument(
        "--stack", default=None, choices=["node-ts", "python", "jvm-spring", "go"],
        help="Explicit stack, bypassing auto-detection entirely — requires --service too. "
             "For a folder shape auto-detection can't recognize (e.g. a library/CLI package).",
    )
    p_index.add_argument("--repository-name", default=None, help="Explicit repository name; avoids collisions when indexing several repos into one shared DB")
    p_index.add_argument("--force", action="store_true", help="Regenerate everything, ignoring file-hash skip")
    p_index.add_argument("--sufficiency-details", action="store_true", help="Print per-route evidence sufficiency as JSON lines")
    p_index.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log every file and function being statically analyzed at DEBUG level (to stderr) — "
             "for diagnosing a crash: the last line printed names exactly where it happened",
    )
    add_backend_args(p_index)
    add_depth_args(p_index)
    p_index.set_defaults(func=_cmd_index)

    p_update = sub.add_parser(
        "update", help="Re-index one already-known service, or every service in a repository",
        epilog="example:\n  orbitkb update orders-service\n  orbitkb update --repository shop\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_update.add_argument(
        "service", nargs="?", default=None, help="Exact name shown by `orbitkb list` (omit when using --repository)",
    )
    p_update.add_argument(
        "--repository", default=None,
        help="Update every service in this repository instead of one by name (see `orbitkb list`)",
    )
    p_update.add_argument("--force", action="store_true", help="Regenerate everything, ignoring file-hash skip")
    p_update.add_argument("--sufficiency-details", action="store_true", help="Print per-route evidence sufficiency as JSON lines")
    p_update.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log every file and function being statically analyzed at DEBUG level (to stderr) — "
             "for diagnosing a crash: the last line printed names exactly where it happened",
    )
    add_backend_args(p_update)
    add_depth_args(p_update)
    p_update.set_defaults(func=_cmd_update)

    p_list = sub.add_parser("list", help="List indexed services")
    p_list.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_list.set_defaults(func=_cmd_list)

    p_status = sub.add_parser(
        "status", help="Show indexing status/history, and recent change surface verifications",
        epilog="examples:\n  orbitkb status\n  orbitkb status orders-service\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_status.add_argument("service", nargs="?", default=None, help="Show one service's indexing history instead of the whole DB's")
    p_status.add_argument("--units", action="store_true", help="Show opaque per-unit indexing measurements for one service")
    p_status.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_status.set_defaults(func=_cmd_status)

    p_remove = sub.add_parser(
        "remove", help="Remove one retired repository and all knowledge it owns",
        epilog="example:\n  orbitkb remove --repository retired-monorepo\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_remove.add_argument("--repository", required=True, help="Exact repository name shown by `orbitkb list`")
    p_remove.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_remove.set_defaults(func=_cmd_remove)

    p_export = sub.add_parser(
        "export", help="Export the database to Markdown or Mermaid diagrams",
        epilog=(
            "examples:\n"
            "  orbitkb export md --out docs/\n"
            "  orbitkb export mermaid --out docs/\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_export.add_argument("format", choices=["md", "mermaid"], help="md: human-readable docs; mermaid: topology.mmd + one er.mmd per service")
    p_export.add_argument("--out", default="docs", help="Output directory (default: docs)")
    p_export.add_argument("--service", default=None, help="Export only this service instead of every indexed one")
    p_export.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_export.set_defaults(func=_cmd_export)

    p_backup = sub.add_parser("backup", help="Create a consistent SQLite knowledge-base backup")
    p_backup.add_argument("--out", type=Path, required=True, help="Destination SQLite backup path")
    p_backup.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Source database path (default: {DEFAULT_DB_PATH})")
    p_backup.set_defaults(func=_cmd_backup)

    p_restore = sub.add_parser("restore", help="Restore a SQLite knowledge-base backup into --db")
    p_restore.add_argument("source", type=Path, help="SQLite backup file to restore")
    p_restore.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Destination database path (default: {DEFAULT_DB_PATH})")
    p_restore.set_defaults(func=_cmd_restore)

    p_analyze = sub.add_parser(
        "analyze", help="Run find_change_surface for a task and print the result as JSON",
        epilog=(
            "examples:\n"
            "  orbitkb analyze \"Add support for Pix in checkout\"\n"
            "  orbitkb analyze \"Add support for Pix in checkout\" --backend claude --db verify/sample_project.db\n"
            "  orbitkb analyze \"xyz internal cleanup\" --repository billing --hint-services notification-service\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_analyze.add_argument("task", help="Free-text engineering task/epic, e.g. \"Add support for Pix in checkout\"")
    p_analyze.add_argument("--hint-services", nargs="+", default=None, help="Anchor the search on these services even without a keyword match")
    p_analyze.add_argument("--repository", default=None, help="Limit analysis to one repository; required when duplicate service names exist")
    add_backend_args(p_analyze)
    p_analyze.set_defaults(func=_cmd_analyze)

    p_context = sub.add_parser(
        "context", help="Build a compact, bounded implementation briefing for a task/epic",
        epilog=(
            "examples:\n"
            "  orbitkb context \"Add support for Pix in checkout\"\n"
            "  orbitkb context \"Add support for Pix in checkout\" --max-services 2 --repository billing\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_context.add_argument("task", help="Free-text engineering task/epic to brief before planning")
    p_context.add_argument("--hint-services", nargs="+", default=None, help="Anchor the impact search on these services")
    p_context.add_argument("--repository", default=None, help="Limit context to one repository; required when duplicate service names exist")
    p_context.add_argument("--max-services", type=int, default=3, help="Compact cards to return (1-5, default: 3)")
    p_context.add_argument("--epic-type", default="unspecified", help="Non-sensitive category for budget calibration (default: unspecified)")
    add_backend_args(p_context)
    p_context.set_defaults(func=_cmd_context)

    p_context_feedback = sub.add_parser(
        "context-feedback", help="Record whether a compact context briefing was sufficient",
    )
    p_context_feedback.add_argument("run_id", type=int, help="telemetry.run_id from `orbitkb context`")
    p_context_feedback.add_argument("outcome", choices=["sufficient", "insufficient", "excessive"], help="Whether the delivered context was sufficient, insufficient or excessive")
    p_context_feedback.add_argument("--note", default=None, help="Optional note; retained only as a one-way digest")
    p_context_feedback.add_argument("--missing-services", nargs="*", default=None, help="Services absent from an insufficient context")
    p_context_feedback.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_context_feedback.set_defaults(func=_cmd_context_feedback)

    p_context_query = sub.add_parser(
        "context-query", help="Record an executed recommended follow-up query",
    )
    p_context_query.add_argument("run_id", type=int, help="telemetry.run_id from `orbitkb context`")
    p_context_query.add_argument("tool", help="Recommended MCP tool that was executed")
    p_context_query.add_argument("--service", default=None, help="Service supplied to the recommended tool, if any")
    p_context_query.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_context_query.set_defaults(func=_cmd_context_query)

    p_context_metrics = sub.add_parser(
        "context-metrics", help="Show privacy-safe context-budget calibration metrics",
    )
    p_context_metrics.add_argument("--epic-type", default=None, help="Filter metrics to one non-sensitive epic category")
    p_context_metrics.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_context_metrics.set_defaults(func=_cmd_context_metrics)

    p_metrics = sub.add_parser(
        "metrics",
        help="Show local operational metrics; add --watch for a live terminal view",
        epilog=(
            "examples:\n"
            "  orbitkb metrics --watch\n"
            f"  {_METRICS_INTERVAL_ENV}=2 orbitkb metrics --watch\n"
            f"  {_METRICS_MIN_INTERVAL_ENV}=0.25 orbitkb metrics --watch --alerts-only\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_metrics.add_argument("--watch", action="store_true", help="Refresh the local read-only view until Ctrl+C")
    p_metrics.add_argument(
        "--interval",
        type=float,
        default=None,
        help=f"Seconds between refreshes in watch mode (default: ${_METRICS_INTERVAL_ENV} or 1.0)",
    )
    p_metrics.add_argument("--alerts-only", action="store_true", help="Show only actionable validation and plan-quality alerts")
    p_metrics.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    p_metrics.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_metrics.set_defaults(func=_cmd_metrics)

    p_context_verify = sub.add_parser(
        "context-verify", help="Compare delivered context cards with services changed in Git",
    )
    p_context_verify.add_argument("run_id", type=int, help="telemetry.run_id from `orbitkb context`")
    p_context_verify.add_argument("--repository", required=True, help="Indexed repository name to verify")
    p_context_verify.add_argument("--since", required=True, help="Git commit before the implementation changes")
    p_context_verify.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_context_verify.set_defaults(func=_cmd_context_verify)

    p_runtime_ingest = sub.add_parser("runtime-ingest", help="Ingest payload-free normalized runtime flow observations")
    p_runtime_ingest.add_argument("service", help="Indexed service receiving the observations")
    p_runtime_ingest.add_argument("source", choices=["otel", "broker"], help="Runtime observation source")
    p_runtime_ingest.add_argument("observations", help="JSON array of {from,to,kind,count}; trace/payload fields are rejected")
    p_runtime_ingest.add_argument("--repository", default=None, help="Repository required for duplicate service names")
    p_runtime_ingest.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_runtime_ingest.set_defaults(func=_cmd_runtime_ingest)

    p_runtime_divergence = sub.add_parser("runtime-divergence", help="Compare runtime observations with static flow facts")
    p_runtime_divergence.add_argument("service", help="Indexed service to compare")
    p_runtime_divergence.add_argument("--repository", default=None, help="Repository required for duplicate service names")
    p_runtime_divergence.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_runtime_divergence.set_defaults(func=_cmd_runtime_divergence)

    p_verify = sub.add_parser(
        "verify", help="Compare a past find_change_surface run against what a repository's commits actually changed",
        epilog=(
            "example:\n"
            "  orbitkb verify 3 --repository my-monorepo --since a1b2c3d --record-feedback\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_verify.add_argument("run_id", type=int, help="run_id from a prior find_change_surface/analyze response")
    p_verify.add_argument("--repository", required=True, help="Repository name, as shown by `orbitkb list`/`--repository-name` at index time")
    p_verify.add_argument("--since", required=True, help="Commit the run was made against; actual changes are `git diff --since..HEAD`")
    p_verify.add_argument("--record-feedback", action="store_true", help="Auto-record confirmed/rejected feedback for the predicted services")
    p_verify.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    p_verify.set_defaults(func=_cmd_verify)

    p_setup = sub.add_parser(
        "setup",
        help="Register orbitkb as an MCP server, and (with --repository) install a reindex hook + agent instructions",
        epilog=(
            "example:\n"
            "  orbitkb setup --repository shop --backend codex\n"
            "  orbitkb setup --client codex\n"
            "  orbitkb setup --remove --repository shop --yes\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_setup.add_argument(
        "--repository", default=None,
        help="Repository name (see `orbitkb list`); also installs the reindex git hook and agent instructions there",
    )
    p_setup.add_argument(
        "--client", action="append", choices=list(_SETUP_CLIENTS), default=None,
        help="Register only this MCP client (repeatable; default: detected clients, or all three if none are)",
    )
    p_setup.add_argument(
        "--scope", choices=["project", "user"], default=None,
        help="Claude Code/Cursor config scope (default: project with --repository, user otherwise)",
    )
    p_setup.add_argument(
        "--force", action="store_true",
        help="Overwrite a conflicting MCP entry instead of reporting it (never affects the git hook)",
    )
    p_setup.add_argument(
        "--remove", action="store_true",
        help="Undo a previous setup: remove the MCP registration, the reindex hook and the agent instructions",
    )
    p_setup.add_argument(
        "--yes", action="store_true",
        help="Skip confirmation: with --remove, actually delete instead of previewing; otherwise, skip the client-selection prompt",
    )
    p_setup.add_argument("--dry-run", action="store_true", help="Print what would be written/installed without touching disk")
    add_backend_args(p_setup)
    p_setup.set_defaults(func=_cmd_setup)

    p_serve = sub.add_parser(
        "serve", help="Run the MCP server (stdio)",
        epilog=(
            "example (register with an MCP client, e.g. Claude CLI/Codex):\n"
            "  orbitkb serve --backend claude\n"
            "  orbitkb serve --db ~/.orbitkb/orbitkb.db --backend codex\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_serve.add_argument("--transport", choices=["stdio"], default="stdio", help="MCP transport (only stdio is supported today)")
    add_backend_args(p_serve)  # find_change_surface is the only tool that uses a backend
    p_serve.set_defaults(func=_cmd_serve)

    return parser


# Grouping for the top-level `-h`/`--help` render only (see _render_top_level_help)
# — purely a display concern, doesn't touch parsing/dispatch. Each subcommand's own
# `--help` page is untouched (still argparse's default RawDescriptionHelpFormatter).
_COMMAND_GROUPS: list[tuple[str, list[str]]] = [
    ("Core Workflow", ["index", "update", "list", "status", "remove"]),
    ("Docs & Backups", ["export", "backup", "restore"]),
    (
        "Change Impact (ask / verify)",
        ["analyze", "context", "context-feedback", "context-query", "context-metrics", "context-verify", "verify"],
    ),
    ("Runtime Evidence", ["runtime-ingest", "runtime-divergence"]),
    ("Operate", ["metrics", "setup", "serve"]),
]

_GLOBAL_OPTIONS: list[tuple[str, str]] = [
    ("-h, --help", "Show this help message and exit."),
    ("--version", "Show the installed version and exit."),
]

_HELP_LINE_WIDTH = 96


def _subcommand_help_texts(sub: argparse._SubParsersAction) -> dict[str, str]:
    """Each command's `help="..."` text (already passed to `add_parser(...)`) is
    kept only on the subparsers action's own choice list, never on the subparser
    itself — this is the same private attribute argparse's own default formatter
    reads to build the "positional arguments" listing this function replaces."""
    return {action.dest: action.help for action in sub._choices_actions}


def _render_aligned_entries(entries: list[tuple[str, str]], column: int) -> list[str]:
    lines = []
    for name, text in entries:
        wrapped = textwrap.wrap(text, width=max(_HELP_LINE_WIDTH - column, 20)) or [""]
        lines.append(f"  {name.ljust(column - 2)}{wrapped[0]}")
        lines.extend(" " * column + line for line in wrapped[1:])
    return lines


def _render_top_level_help(sub: argparse._SubParsersAction) -> str:
    help_texts = _subcommand_help_texts(sub)
    all_names = [name for _, names in _COMMAND_GROUPS for name in names]
    column = max(len(name) for name in all_names) + 2 + 4  # 2-space margin + 4-space gap

    lines = [
        "usage: orbitkb [-h] [--version] <command> ...",
        "",
        "Global Options:",
        *_render_aligned_entries(_GLOBAL_OPTIONS, column),
        "",
    ]
    for title, names in _COMMAND_GROUPS:
        lines.append(f"{title}:")
        lines.extend(_render_aligned_entries([(name, help_texts[name]) for name in names], column))
        lines.append("")

    return "\n".join(lines) + "\n" + _TOP_LEVEL_EPILOG


def main(argv: list[str] | None = None) -> int:
    # `index`/`update` shell into tree-sitter, a native extension: a use-after-free there
    # raises SIGBUS/SIGSEGV, which Python can't catch, and the OS crash reporter only shows
    # C frames inside the interpreter -- never which Python source line was running. This is
    # the one thing that can print that frame at the moment of the signal, so it has to be
    # armed up front; there's no enabling it after a process is already dead. `sys.__stderr__`
    # (not `sys.stderr`) because faulthandler needs a real fd, and test harnesses commonly
    # replace `sys.stderr` with a captured stream that has none.
    if sys.__stderr__ is not None:
        faulthandler.enable(file=sys.__stderr__)
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        print(_render_top_level_help(sub))
        return 0
    args = parser.parse_args(argv)
    # A real SIGBUS was found happening *inside* the cyclic collector's own traversal
    # (gc_collect_main -> subtype_traverse) of a long-lived, cached tree-sitter `Tree`,
    # triggered automatically by the interpreter's allocation-count threshold -- not by
    # any use-after-free on our end (that class of bug was fixed separately; this one
    # crashed a `Tree` still alive and referenced). `Tree`/`Node` objects hold no
    # reference cycles, so the cyclic collector buys nothing walking them; disabling it
    # for the command's duration sidesteps that call path entirely. Regular refcounting
    # (tp_dealloc, not gc_collect_main) still frees every object normally the instant
    # its refcount hits zero, so nothing leaks.
    gc.disable()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ncancelado pelo usuário (Ctrl+C)", file=sys.stderr)
        return 130
    finally:
        gc.enable()


if __name__ == "__main__":
    raise SystemExit(main())
