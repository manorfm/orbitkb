#!/usr/bin/env python3
"""Run the deterministic cross-stack static-analysis golden corpus.

Usage:
    python scripts/run_static_evaluation.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from benchmark.static_evaluation import CASES, run_static_evaluation


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        report = run_static_evaluation(CASES, Path(directory))
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0 if report.passes_quality_gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
