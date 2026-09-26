"""Idempotent, non-destructive registration of `orbitkb serve` as an MCP server
for Claude Code, Cursor and Codex CLI.

Each client is registered by writing directly to its own config file rather
than shelling out to that client's CLI (`claude mcp add`/`codex mcp add`):
Cursor has no CLI at all, and a single code path works uniformly across all
three regardless of which client CLIs happen to be installed.

The exact schemas below were confirmed against real local config files while
building this module, not assumed:
- Claude Code: top-level `mcpServers` in both `.mcp.json` (project scope) and
  `~/.claude.json` (user scope), entries shaped
  `{"type": "stdio", "command": ..., "args": [...]}`.
- Cursor: top-level `mcpServers` in both `.cursor/mcp.json` (project) and
  `~/.cursor/mcp.json` (user), entries shaped `{"command": ..., "args": [...]}`
  (no `"type"` field).
- Codex CLI: always user-level `~/.codex/config.toml`, `[mcp_servers.<name>]`
  tables with `command`/`args` — no project scope exists for this client.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import tomlkit

from orbitkb.setup.actions import SetupAction

_ENTRY_KEY = "orbitkb"


def mcp_command_line(
    backend: str | None, model: str | None, claude_bare: bool, codex_api_key: bool, db: Path,
) -> tuple[str, list[str]]:
    """Build the `orbitkb serve ...` invocation to register with an MCP client,
    from the same flags `orbitkb setup` accepts. Resolves an absolute path when
    possible, since a client's inherited PATH may not include `orbitkb`.
    """
    command = shutil.which("orbitkb") or "orbitkb"
    args = ["serve"]
    if backend:
        args += ["--backend", backend]
    if model:
        args += ["--model", model]
    if claude_bare:
        args.append("--claude-bare")
    if codex_api_key:
        args.append("--codex-api-key")
    args += ["--db", str(db)]
    return command, args


def resolve_claude_code_config_path(repository_root: Path, scope: str, home: Path | None = None) -> Path:
    if scope == "project":
        return repository_root / ".mcp.json"
    if scope == "user":
        return (home or Path.home()) / ".claude.json"
    raise ValueError(f"unknown scope: {scope!r} (expected 'project' or 'user')")


def resolve_cursor_config_path(repository_root: Path, scope: str, home: Path | None = None) -> Path:
    if scope == "project":
        return repository_root / ".cursor" / "mcp.json"
    if scope == "user":
        return (home or Path.home()) / ".cursor" / "mcp.json"
    raise ValueError(f"unknown scope: {scope!r} (expected 'project' or 'user')")


def resolve_codex_config_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".codex" / "config.toml"


def _merge_json_mcp_server(
    path: Path, entry: dict, *, client: str, scope: str, force: bool, dry_run: bool,
) -> SetupAction:
    file_existed = path.exists()
    if file_existed:
        try:
            existing = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            return SetupAction(
                category="mcp", client=client, scope=scope, path=path, status="conflict",
                detail=f"existing file is not valid JSON ({exc}); left untouched",
            )
        if not isinstance(existing, dict):
            return SetupAction(
                category="mcp", client=client, scope=scope, path=path, status="conflict",
                detail="existing file's top level is not a JSON object; left untouched",
            )
    else:
        existing = {}

    servers = existing.get("mcpServers", {})
    if not isinstance(servers, dict):
        return SetupAction(
            category="mcp", client=client, scope=scope, path=path, status="conflict",
            detail="existing 'mcpServers' key is not a JSON object; left untouched",
        )

    current = servers.get(_ENTRY_KEY)
    if current == entry:
        return SetupAction(category="mcp", client=client, scope=scope, path=path, status="unchanged")
    if current is not None and not force:
        snippet = json.dumps({_ENTRY_KEY: entry}, indent=2)
        return SetupAction(
            category="mcp", client=client, scope=scope, path=path, status="conflict",
            detail=(
                f"'{_ENTRY_KEY}' is already registered here with a different command; "
                f"paste this into {path} under \"mcpServers\" to update it manually, "
                f"or re-run `orbitkb setup` with --force:\n{snippet}"
            ),
        )

    status = "created" if not file_existed else "updated"
    if not dry_run:
        servers = dict(servers)
        servers[_ENTRY_KEY] = entry
        existing["mcpServers"] = servers
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return SetupAction(category="mcp", client=client, scope=scope, path=path, status=status)


def write_claude_code_config(
    repository_root: Path, scope: str, command: str, args: list[str],
    *, home: Path | None = None, force: bool = False, dry_run: bool = False,
) -> SetupAction:
    path = resolve_claude_code_config_path(repository_root, scope, home)
    entry = {"type": "stdio", "command": command, "args": args}
    return _merge_json_mcp_server(path, entry, client="claude-code", scope=scope, force=force, dry_run=dry_run)


def write_cursor_config(
    repository_root: Path, scope: str, command: str, args: list[str],
    *, home: Path | None = None, force: bool = False, dry_run: bool = False,
) -> SetupAction:
    path = resolve_cursor_config_path(repository_root, scope, home)
    entry = {"command": command, "args": args}
    return _merge_json_mcp_server(path, entry, client="cursor", scope=scope, force=force, dry_run=dry_run)


def write_codex_config(
    command: str, args: list[str], *, home: Path | None = None, force: bool = False, dry_run: bool = False,
) -> SetupAction:
    path = resolve_codex_config_path(home)
    file_existed = path.exists()
    if file_existed:
        try:
            doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        except tomlkit.exceptions.TOMLKitError as exc:
            return SetupAction(
                category="mcp", client="codex", scope="user", path=path, status="conflict",
                detail=f"existing file is not valid TOML ({exc}); left untouched",
            )
    else:
        doc = tomlkit.document()

    servers = doc.get("mcp_servers")
    current = dict(servers[_ENTRY_KEY]) if servers is not None and _ENTRY_KEY in servers else None
    entry = {"command": command, "args": args}
    if current == entry:
        return SetupAction(category="mcp", client="codex", scope="user", path=path, status="unchanged")
    if current is not None and not force:
        snippet = f'[mcp_servers.{_ENTRY_KEY}]\ncommand = "{command}"\nargs = {json.dumps(args)}\n'
        return SetupAction(
            category="mcp", client="codex", scope="user", path=path, status="conflict",
            detail=(
                f"'{_ENTRY_KEY}' is already registered here with a different command; "
                f"paste this into {path} to update it manually, "
                f"or re-run `orbitkb setup` with --force:\n{snippet}"
            ),
        )

    status = "created" if not file_existed else "updated"
    if not dry_run:
        if servers is None:
            servers_table = tomlkit.table(is_super_table=True)
            doc["mcp_servers"] = servers_table
        else:
            servers_table = servers
        entry_table = tomlkit.table()
        entry_table["command"] = command
        entry_table["args"] = args
        servers_table[_ENTRY_KEY] = entry_table
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return SetupAction(category="mcp", client="codex", scope="user", path=path, status=status)
