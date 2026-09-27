#!/usr/bin/env python3
"""Print OrbitKB's deterministic readiness evidence and remaining conditions."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from benchmark.readiness import run_readiness_audit
from benchmark.real_corpus import (
    CorpusValidationError,
    RealCorpusQualityGate,
    load_real_corpus,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run OrbitKB's privacy-safe readiness audit.")
    parser.add_argument("--corpus", type=Path, help="Path to a local redacted real-change corpus manifest")
    parser.add_argument("--required-stack", action="append", default=[], help="Stack that must satisfy the corpus gate")
    parser.add_argument("--min-cases-per-stack", type=int, help="Minimum corpus cases required per stack")
    parser.add_argument("--min-precision", type=float, help="Minimum corpus precision required per stack")
    parser.add_argument("--min-recall", type=float, help="Minimum corpus recall required per stack")
    args = parser.parse_args(argv)
    gate_options_supplied = any(
        (
            args.required_stack,
            args.min_cases_per_stack is not None,
            args.min_precision is not None,
            args.min_recall is not None,
        )
    )
    real_corpus = None
    real_corpus_gate = None
    if args.corpus is None and gate_options_supplied:
        parser.error("corpus quality-gate options require --corpus")
    if args.corpus is not None:
        missing = [
            name
            for name, value in (
                ("--required-stack", args.required_stack),
                ("--min-cases-per-stack", args.min_cases_per_stack),
                ("--min-precision", args.min_precision),
                ("--min-recall", args.min_recall),
            )
            if not value and value != 0
        ]
        if missing:
            parser.error(f"--corpus requires {', '.join(missing)}")
        try:
            real_corpus = load_real_corpus(args.corpus)
            real_corpus_gate = RealCorpusQualityGate(
                required_stacks=frozenset(args.required_stack),
                minimum_cases_per_stack=args.min_cases_per_stack,
                minimum_precision=args.min_precision,
                minimum_recall=args.min_recall,
            )
        except CorpusValidationError as error:
            parser.error(str(error))
    with tempfile.TemporaryDirectory() as directory:
        audit = run_readiness_audit(Path(directory), real_corpus, real_corpus_gate)
    print(json.dumps(audit.as_dict(), sort_keys=True))
    return 0 if audit.status == "conditional" else 1


if __name__ == "__main__":
    raise SystemExit(main())
