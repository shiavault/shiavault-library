#!/usr/bin/env python3
"""Verify that normalize_markdown.py preserves every word of every book.

Compares the letter/digit content of each file before and after normalization.
Entity decoding and the page-reference repair legitimately change characters, so
those two passes are applied to the "before" side as well; everything else must
come out character-identical.
"""

from __future__ import annotations

import glob
import re
import sys
from collections import Counter

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from normalize_markdown import (  # noqa: E402
    _protect_tables,
    _restore_tables,
    fix_entities,
    fix_page_refs,
    normalize,
)

# Tags the normalizer is expected to remove entirely.
STRIPPED_TAGS = re.compile(
    r"\\?</?(?:blockquote|p|span|div|sup|sub|font|a|em|i|strong|b|br|h[1-6])\b[^>]*?\\?>"
)
NON_TEXT = re.compile(r"[^0-9A-Za-z؀-ۿݐ-ݿ]+")


def signature(text: str) -> str:
    return NON_TEXT.sub("", STRIPPED_TAGS.sub(" ", text))


def main() -> int:
    files = sorted(glob.glob("books/**/*.md", recursive=True))
    failures = []
    for path in files:
        with open(path, encoding="utf-8") as handle:
            original = handle.read()
        # Mirror the normalizer's table shielding, otherwise entities and page
        # refs inside <table> blockswould differ purely because of the shield.
        shielded, tables = _protect_tables(original)
        baseline = _restore_tables(
            fix_page_refs(fix_entities(shielded, Counter()), Counter()), tables
        )
        updated = normalize(original, Counter())
        if signature(baseline) != signature(updated):
            failures.append(path)

    print(f"files verified : {len(files)}")
    print(f"text mismatches: {len(failures)}")
    for path in failures[:20]:
        print("  ", path)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
