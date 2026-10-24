#!/usr/bin/env python3
"""End-to-end check: every word in the committed books survives normalization.

Reads each book file as it exists in a git revision (default HEAD) and compares
its letter/digit content against the working tree. Entity decoding and the
page-reference repair legitimately change characters, so they are applied to the
committed side too; everything else must match exactly.
"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from normalize_markdown import _protect_tables, _restore_tables, fix_entities, fix_page_refs  # noqa: E402
from verify_normalization import signature  # noqa: E402


def main() -> int:
    rev = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", rev, "books/"],
        capture_output=True, text=True, check=True,
    ).stdout.split("\n")
    paths = [p for p in listing if p.endswith(".md")]

    failures = []
    for path in paths:
        committed = subprocess.run(
            ["git", "show", f"{rev}:{path}"], capture_output=True, check=True
        ).stdout.decode("utf-8", "replace")
        shielded, tables = _protect_tables(committed)
        baseline = _restore_tables(
            fix_page_refs(fix_entities(shielded, Counter()), Counter()), tables
        )
        try:
            with open(path, encoding="utf-8") as handle:
                current = handle.read()
        except FileNotFoundError:
            failures.append((path, "missing in working tree"))
            continue
        if signature(baseline) != signature(current):
            failures.append((path, "text differs"))

    print(f"files compared vs {rev}: {len(paths)}")
    print(f"mismatches            : {len(failures)}")
    for path, why in failures[:20]:
        print("  ", path, "-", why)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
