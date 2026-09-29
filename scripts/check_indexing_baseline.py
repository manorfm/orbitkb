"""Check synthetic indexing output against the frozen public-output baseline."""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from benchmark.indexing_baseline import collect_baseline, compare_baseline

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = PROJECT_ROOT / "tests" / "golden" / "indexing_baseline.json"
SAMPLE_SOURCE = PROJECT_ROOT / "verify" / "sample_project"
LANGUAGE_SOURCE = PROJECT_ROOT / "verify" / "language_corpus"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="orbitkb-baseline-") as directory:
        actual = collect_baseline((SAMPLE_SOURCE, LANGUAGE_SOURCE), Path(directory))
    changes = compare_baseline(actual, args.baseline)
    if changes:
        for change in changes:
            print(change)
        return 1
    print("Indexing baseline matched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
