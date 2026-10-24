#!/usr/bin/env python3
"""Compare what a reader actually DISPLAYS, before and after normalization.

The source-level check (verify_against_head.py) proves no characters were lost
from the Markdown. That is not the same as proving nothing was lost from the
*page*: a footnote definition, for example, is removed from the body by every
footnote-aware renderer and shown only where a matching reference appears. Text
can survive in the file and still vanish on screen.

This script renders both revisions with CommonMark + footnotes and compares the
visible text, so that class of regression cannot pass unnoticed.

Usage:
    python3 tools/verify_rendered.py <old-tree> [--min-ratio 0.98]
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin

MD = MarkdownIt("commonmark").use(footnote_plugin)
TAG = re.compile(r"<[^>]+>")
BACKREF = re.compile(r"[↩︎↩︎️]")


def visible(markdown_text: str) -> str:
    """Text a reader would see, with tags and footnote back-links removed."""
    html = MD.render(markdown_text)
    text = BACKREF.sub(" ", TAG.sub(" ", html))
    return " ".join(text.split())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old_tree", help="working tree holding the previous revision")
    parser.add_argument("--min-ratio", type=float, default=0.98)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    losses = []
    checked = 0
    for path in sorted(glob.glob("books/**/*.md", recursive=True)):
        old_path = os.path.join(args.old_tree, path)
        if not os.path.exists(old_path):
            continue
        with open(old_path, encoding="utf-8", errors="replace") as handle:
            before = visible(handle.read())
        with open(path, encoding="utf-8", errors="replace") as handle:
            after = visible(handle.read())
        checked += 1
        if not before:
            continue
        ratio = len(after) / len(before)
        if ratio < args.min_ratio:
            losses.append((ratio, len(before), len(after), path))

    losses.sort()
    print(f"files rendered : {checked}")
    print(f"files losing >{(1 - args.min_ratio) * 100:.0f}% of visible text: {len(losses)}")
    for ratio, before_len, after_len, path in losses[: args.limit]:
        print(f"  {ratio:6.1%}  {before_len:>7} -> {after_len:<7} {path}")
    return 1 if losses else 0


if __name__ == "__main__":
    sys.exit(main())
