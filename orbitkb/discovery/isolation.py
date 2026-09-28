"""Runs a callable in a subprocess so a native crash there (SIGSEGV/SIGBUS from a C
extension bug -- tree-sitter's Kotlin grammar binding has one, reproduced on a real
service: heap corruption from a native parse/free cycle that only manifests much
later, in unrelated code, see the incident this exists for) kills only that
subprocess, not the whole `orbitkb index`/`update` run. A `try/except` can never
catch a native crash; only a process boundary can contain it.
"""
from __future__ import annotations

import faulthandler
import logging
import multiprocessing
import signal
import sys
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def _call_and_put(func: Callable[..., object], args: tuple, queue: multiprocessing.Queue, log_level: int) -> None:
    # `multiprocessing`'s "spawn" context starts a brand-new interpreter that never
    # runs the parent's `main()` -- neither `faulthandler.enable()` nor `--verbose`'s
    # `logging.basicConfig()` carry over automatically, so a crash here would silently
    # lose the exact diagnostics (Python source line, per-file DEBUG trail) this whole
    # module exists to still capture even when the crash itself is expected.
    if sys.__stderr__ is not None:
        faulthandler.enable(file=sys.__stderr__)
    logging.basicConfig(level=log_level, format="%(message)s", stream=sys.stderr, force=True)
    try:
        queue.put(("ok", func(*args)))
    except Exception as exc:
        queue.put(("error", str(exc)))


def run_isolated(func: Callable[..., T], *args: object, timeout: float = 60.0) -> tuple[T | None, str | None]:
    """Runs `func(*args)` in a fresh subprocess and returns `(result, None)` on
    success, or `(None, reason)` if the subprocess crashed, hung, or raised.

    `func` must be a plain, top-level function (picklable by reference): any
    native/unpicklable state it needs (a tree-sitter `Parser`, a
    `StaticAnalysisEngine`) must be built inside the subprocess, never passed in as
    an argument, since native objects generally can't cross a process boundary.

    Corrupted heap state doesn't only crash -- reproduced directly, it also hung a
    subprocess indefinitely (sleeping, zero CPU, never returning) with no signal to
    catch. `process.join()` alone waits forever in that case, so `timeout` bounds it:
    past it, the process is killed and treated the same as a crash.
    """
    ctx = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue = ctx.Queue()
    log_level = logging.getLogger().getEffectiveLevel()
    process = ctx.Process(target=_call_and_put, args=(func, args, queue, log_level))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
        return None, f"timed out after {timeout:.0f}s and was killed"
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
