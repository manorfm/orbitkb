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
    args = parser.parse_args(argv)
    try:
        corpus = load_real_corpus(args.corpus)
    except CorpusValidationError as error:
        parser.error(str(error))
    report = evaluate_real_corpus(corpus) if args.evaluate else corpus
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
