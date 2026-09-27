"""Runs a callable in a subprocess so a native crash there (SIGSEGV/SIGBUS from a C
extension bug -- tree-sitter's Kotlin grammar binding has one, reproduced on a real
service: heap corruption from a native parse/free cycle that only manifests much
later, in unrelated code, see the incident this exists for) kills only that
subprocess, not the whole `orbitkb index`/`update` run. A `try/except` can never
catch a native crash; only a process boundary can contain it.
"""
from __future__ import annotations

import multiprocessing
import signal
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def _call_and_put(func: Callable[..., object], args: tuple, queue: multiprocessing.Queue) -> None:
    try:
        queue.put(("ok", func(*args)))
    except Exception as exc:
        queue.put(("error", str(exc)))


def run_isolated(func: Callable[..., T], *args: object) -> tuple[T | None, str | None]:
    """Runs `func(*args)` in a fresh subprocess and returns `(result, None)` on
    success, or `(None, reason)` if the subprocess crashed or raised.

    `func` must be a plain, top-level function (picklable by reference): any
    native/unpicklable state it needs (a tree-sitter `Parser`, a
    `StaticAnalysisEngine`) must be built inside the subprocess, never passed in as
    an argument, since native objects generally can't cross a process boundary.
    """
    ctx = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue = ctx.Queue()
    process = ctx.Process(target=_call_and_put, args=(func, args, queue))
    process.start()
    process.join()
    if process.exitcode == 0:
        outcome, payload = queue.get()
        return (payload, None) if outcome == "ok" else (None, payload)
    if process.exitcode is not None and process.exitcode < 0:
        try:
            name = signal.Signals(-process.exitcode).name
        except ValueError:
            name = str(-process.exitcode)
        return None, f"crashed with signal {name}"
    return None, f"exited with code {process.exitcode}"
