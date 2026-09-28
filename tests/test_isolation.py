"""`run_isolated()` exists because a native crash (SIGSEGV/SIGBUS from a C extension
bug -- tree-sitter's Kotlin grammar binding has a real, reproducible one, see
test_engine_tree_lifetime.py and its Go/JVM descendants) can never be caught by
`try/except`: the whole process dies. Running the risky call in a subprocess turns
that into a detectable, non-fatal outcome for the parent.
"""
from __future__ import annotations

import time

from orbitkb.discovery.isolation import run_isolated


def _add(a: int, b: int) -> int:
    return a + b


def _boom() -> None:
    raise ValueError("deliberate failure")


def _segfault() -> None:
    import faulthandler
    faulthandler._sigsegv()


def _hang() -> None:
    while True:
        time.sleep(1)


def test_run_isolated_returns_the_function_result_on_success():
    result, reason = run_isolated(_add, 2, 3)

    assert result == 5
    assert reason is None


def test_run_isolated_reports_a_raised_exception_without_crashing_the_caller():
    result, reason = run_isolated(_boom)

    assert result is None
    assert reason is not None
    assert "deliberate failure" in reason


def test_run_isolated_detects_a_native_crash_without_crashing_the_caller():
    result, reason = run_isolated(_segfault)

    assert result is None
    assert reason is not None
    assert "signal" in reason.lower()


def test_run_isolated_kills_a_hung_subprocess_instead_of_blocking_forever():
    """Corrupted heap state doesn't only crash -- reproduced directly against a real
    service, it hung a subprocess indefinitely (sleeping, zero CPU, no signal, never
    returning). `process.join()` alone waits forever in that case; `timeout` bounds it.
    """
    result, reason = run_isolated(_hang, timeout=1)

    assert result is None
    assert reason is not None
    assert "timed out" in reason.lower()
