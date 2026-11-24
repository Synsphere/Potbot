#!/usr/bin/env python3
"""Require successful, disjoint PostgreSQL partitions with complete collection coverage."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from potbot.evaluation.postgres_test_shards import verify_postgres_shards


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if not 1 <= len(args.reports) <= 16:
            raise ValueError("Invalid receipt count")
        reports = []
        for path in args.reports:
            with path.open("rb") as handle:
                raw = handle.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("Oversized receipt")
            reports.append(json.loads(raw))
        result = verify_postgres_shards(reports, args.source_commit)
        args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, TypeError, KeyError, OSError, UnicodeError):
        parser.exit(
            1, "PostgreSQL partition coverage failed; inspect the retained shard receipts.\n"
        )


if __name__ == "__main__":
    raise SystemExit(main())
