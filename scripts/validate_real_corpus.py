#!/usr/bin/env python3
"""Validate a local, redacted real-change corpus manifest.

Usage:
    python scripts/validate_real_corpus.py --corpus /secure/path/corpus.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from benchmark.real_corpus import (
    CorpusValidationError,
    RealCorpusQualityGate,
    evaluate_real_corpus,
    load_real_corpus,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a redacted real-change corpus manifest.")
    parser.add_argument("--corpus", type=Path, required=True, help="Path to the local JSON corpus manifest")
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Measure predicted versus changed unit references by stack",
    )
    parser.add_argument("--gate", action="store_true", help="Fail when explicit corpus quality requirements are unmet")
    parser.add_argument("--required-stack", action="append", default=[], help="Stack that must satisfy the gate")
    parser.add_argument("--min-cases-per-stack", type=int, help="Minimum cases required for each stack")
    parser.add_argument("--min-precision", type=float, help="Minimum precision required for each stack")
    parser.add_argument("--min-recall", type=float, help="Minimum recall required for each stack")
    args = parser.parse_args(argv)
    try:
        corpus = load_real_corpus(args.corpus)
    except CorpusValidationError as error:
        parser.error(str(error))
    gate_options_supplied = any(
        (
            args.required_stack,
            args.min_cases_per_stack is not None,
            args.min_precision is not None,
            args.min_recall is not None,
        )
    )
    if args.gate:
        if not args.evaluate:
            parser.error("--gate requires --evaluate")
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
            parser.error(f"--gate requires {', '.join(missing)}")
        try:
            gate = RealCorpusQualityGate(
                required_stacks=frozenset(args.required_stack),
                minimum_cases_per_stack=args.min_cases_per_stack,
                minimum_precision=args.min_precision,
                minimum_recall=args.min_recall,
            )
        except CorpusValidationError as error:
            parser.error(str(error))
        result = evaluate_real_corpus(corpus).apply_gate(gate)
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0 if result.passed else 1
    if gate_options_supplied:
        parser.error("quality-gate options require --gate")
    report = evaluate_real_corpus(corpus) if args.evaluate else corpus
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
