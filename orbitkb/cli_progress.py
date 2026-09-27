"""Rich-based terminal progress for `orbitkb index`/`update`.

Kept separate from generation/orchestrator.py so the generation logic never depends
on a UI library — it only calls the small ProgressReporter protocol.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from orbitkb.db.repositories import local_index_progress

_STATUS_STYLE = {
    "ok": "[green]ok[/green]",
    "failed": "[bold red]falhou[/bold red]",
    "skipped": "[dim]sem mudanças[/dim]",
}


class RichProgressReporter:
    """One growing bar per service; finished services stay on screen at 100%, so a
    multi-service run visibly fills up from top to bottom. TimeElapsedColumn keeps
    ticking on the active row even while a single slow LLM call is in flight, so a
    stalled step is visibly still "alive" (elapsed climbing) versus truly hung
    (spinner frozen too, which only happens if the process itself died).
    """

    def __init__(self) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.fields[service]}[/bold]"),
            BarColumn(),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[detail]}"),
            TimeElapsedColumn(),
        )
        self._tasks: dict[str, int] = {}

    def __enter__(self) -> "RichProgressReporter":
        self._progress.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._progress.__exit__(*exc)

    def service_started(self, service: str, total_units: int) -> None:
        task_id = self._progress.add_task(service, total=max(total_units, 1), service=service, detail="iniciando…")
        self._tasks[service] = task_id

    def unit_started(self, service: str, label: str) -> None:
        self._progress.update(self._tasks[service], detail=f"gerando: {label}…")

    def unit_finished(self, service: str, label: str, status: str) -> None:
        style = _STATUS_STYLE.get(status, status)
        self._progress.update(self._tasks[service], advance=1, detail=f"{label}: {style}")

    def service_finished(self, service: str) -> None:
        self._progress.update(self._tasks[service], detail="[bold green]concluído[/bold green]")


class LocalIndexProgressReporter:
    """Best-effort aggregate progress for `orbitkb metrics --watch`.

    It deliberately publishes only a service name, normalized stage and counters.
    A short-timeout separate connection means a busy index transaction can never be
    delayed merely to update the optional monitor.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def service_started(self, service: str, total_units: int) -> None:
        self._record(local_index_progress.start, service, total_units)

    def unit_started(self, service: str, label: str) -> None:
        self._record(local_index_progress.stage_started, service, _stage_for_label(label))

    def unit_finished(self, service: str, label: str, status: str) -> None:
        del status
        self._record(local_index_progress.unit_finished, service, _stage_for_label(label))

    def service_finished(self, service: str) -> None:
        self._record(local_index_progress.finish, service)

    def _record(self, action, *args: object) -> None:
        try:
            with closing(sqlite3.connect(str(self._db_path), timeout=0.05)) as conn:
                conn.row_factory = sqlite3.Row
                action(conn, *args)
        except sqlite3.Error:
            pass


class CompositeProgressReporter:
    """Fan out progress callbacks so terminal and local monitor stay consistent."""

    def __init__(self, *reporters: object) -> None:
        self._reporters = reporters

    def service_started(self, service: str, total_units: int) -> None:
        for reporter in self._reporters:
            reporter.service_started(service, total_units)

    def unit_started(self, service: str, label: str) -> None:
        for reporter in self._reporters:
            reporter.unit_started(service, label)

    def unit_finished(self, service: str, label: str, status: str) -> None:
        for reporter in self._reporters:
            reporter.unit_finished(service, label, status)

    def service_finished(self, service: str) -> None:
        for reporter in self._reporters:
            reporter.service_finished(service)


def _stage_for_label(label: str) -> str:
    if label.startswith("component "):
        return "component_analysis"
    if label == "persistence":
        return "persistence_analysis"
    if label == "messaging":
        return "messaging_analysis"
    if label == "overview":
        return "overview_generation"
    return "endpoint_analysis"
