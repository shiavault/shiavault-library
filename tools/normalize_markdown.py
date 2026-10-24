#!/usr/bin/env python3
"""Normalize the book Markdown so it renders correctly in plain CommonMark readers.

The corpus was machine-converted from HTML and carries several artefacts that
render badly (or literally) in Markdown readers such as Shia Companion:

1. Arabic quotes wrapped in raw ``<blockquote dir="rtl"><p>...</p></blockquote>``
   instead of Markdown ``>`` quotes. Readers that do not pass raw HTML through
   show the tags as text.
2. The same quote split across a Markdown ``>`` line *and* a following HTML
   blockquote, which tears one sentence into two quote boxes.
3. Standalone ``<p dir="rtl">...</p>`` paragraphs.
4. Footnote definitions missing their colon (``[^1] text`` -> ``[^1]: text``),
   which leaves a dead literal marker in the page.
5. Citations where a page number was mistakenly linked as a footnote
   (``vol.3 p.[^289]:`` -> ``vol.3 p.289.``).
6. Stray HTML entities (``&quot;``, ``&amp;``, ``&endash;``).

Text direction is intentionally *not* encoded in the Markdown. The reader is
expected to set ``dir="auto"`` on rendered blocks so Arabic lays out RTL from
its own characters, which is what the ~20k quotes already written as plain
Markdown rely on.

HTML tables are left untouched: they carry layout meaning that Markdown pipe
tables cannot always express, and they are rare (77 files).

Usage:
    python3 tools/normalize_markdown.py [--check] [path ...]
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from collections import Counter

# --- patterns ---------------------------------------------------------------

ENTITIES = {"&quot;": '"', "&endash;": "–", "&amp;": "&"}

# A page/volume number that was turned into a footnote reference. The trailing
# colon is the original sentence-ending period, mis-converted.
MANGLED_PAGE_REF = re.compile(r"\b(p|pp|vol|no)\.\[\^(\d+)\](:)?")

TABLE_BLOCK = re.compile(r"<table\b.*?</table>", re.S)

BLOCKQUOTE_OPEN = re.compile(r"^\s*<blockquote\b[^>]*>\s*$")
BLOCKQUOTE_CLOSE = re.compile(r"^\s*</blockquote>\s*$")
P_OPEN = re.compile(r"^\s*<p\b[^>]*>\s*$")
P_CLOSE = re.compile(r"^\s*</p>\s*$")

FOOTNOTE_LINE = re.compile(r"^\[\^(\d+)\](:)?(\s.*)?$")

# A reference is a marker that is not sitting at the start of its own line, i.e.
# one used from inside a sentence rather than opening a definition.
INLINE_REFERENCE = re.compile(r"(?<!^)\[\^(\d+)\]", re.M)

# Below this share of locally referenced definitions a file is treated as a
# standalone notes chapter rather than a footnote section.
LOCAL_REFERENCE_THRESHOLD = 0.5

# Pandoc escaped some tags as ``\<em\>``. Those render as literal text in every
# reader, so they are unescaped first and then handled like any other tag.
ESCAPED_TAG = re.compile(r"\\<(/?[a-zA-Z][^<>]*?)\\>")

# ``<p dir="rtl">`` opened in the middle of a line splits a word or a phrase in
# two. The inner text is spliced straight back in, with no separator added.
INLINE_P_SPLIT = re.compile(r"(?<=\S)([ \t]*)<p\b[^>]*>[ \t]*\n?\s*(.*?)\s*</p>", re.S)

INLINE_BLOCKQUOTE = re.compile(r"<blockquote\b[^>]*>\s*(.*?)\s*</blockquote>", re.S)
HEADING_TAG = re.compile(r"<h([1-6])\b[^>]*>\s*(.*?)\s*</h\1>", re.S)
ANCHOR = re.compile(r"<a\b[^>]*>\s*(.*?)\s*</a>", re.S)
LINKED_ANCHOR = re.compile(r"<a\b[^>]*href=\"(?!\\?#)([^\"]+)\"[^>]*>\s*(.*?)\s*</a>", re.S)

# Wrappers that carry no meaning in Markdown: keep the text, drop the tag.
UNWRAP = re.compile(r"</?(?:span|div|sup|sub|font|a)\b[^>]*>")
LINE_BREAK = re.compile(r"<br\s*/?>")
ORPHAN_P_LINE = re.compile(r"^[ \t]*</?p\b[^>]*>[ \t]*\n", re.M)
EMPHASIS = [
    (re.compile(r"<(?:em|i)\b[^>]*>(\s*)(.*?)(\s*)</(?:em|i)>", re.S), "*"),
    (re.compile(r"<(?:strong|b)\b[^>]*>(\s*)(.*?)(\s*)</(?:strong|b)>", re.S), "**"),
]


def _protect_tables(text: str) -> tuple[str, list[str]]:
    """Replace <table>...</table> blocks with placeholders so they pass through."""
    stash: list[str] = []

    def grab(match: re.Match) -> str:
        stash.append(match.group(0))
        return f"\x00TABLE{len(stash) - 1}\x00"

    return TABLE_BLOCK.sub(grab, text), stash


def _restore_tables(text: str, stash: list[str]) -> str:
    for i, block in enumerate(stash):
        text = text.replace(f"\x00TABLE{i}\x00", block)
    return text


def fix_entities(text: str, stats: Counter) -> str:
    for entity, char in ENTITIES.items():
        count = text.count(entity)
        if count:
            stats["entities"] += count
            text = text.replace(entity, char)
    return text


def fix_page_refs(text: str, stats: Counter) -> str:
    def repl(match: re.Match) -> str:
        stats["page_refs"] += 1
        label, number, colon = match.groups()
        return f"{label}.{number}." if colon else f"{label}.{number}"

    return MANGLED_PAGE_REF.sub(repl, text)


def _collect_html_block(lines: list[str], start: int, close: re.Pattern) -> tuple[list[str], int]:
    """Return (inner lines, index of the closing line) for an HTML block."""
    i = start + 1
    inner: list[str] = []
    while i < len(lines) and not close.match(lines[i]):
        inner.append(lines[i])
        i += 1
    return inner, i


def _quote_text(inner: list[str]) -> list[str]:
    """Strip <p> wrappers and blank padding from blockquote/paragraph innards."""
    body = [l for l in inner if not P_OPEN.match(l) and not P_CLOSE.match(l)]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return [l.rstrip() for l in body]


def fix_html_blocks(text: str, stats: Counter) -> str:
    """Convert raw <blockquote>/<p dir=rtl> blocks to Markdown, merging split quotes."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if BLOCKQUOTE_OPEN.match(line):
            inner, close_idx = _collect_html_block(lines, i, BLOCKQUOTE_CLOSE)
            if close_idx >= len(lines):  # unclosed: leave the block alone
                out.append(line)
                i += 1
                continue
            body = _quote_text(inner)
            stats["blockquotes"] += 1

            # If the previous emitted line is a Markdown quote, this block is the
            # tail of a quote that was split in two. Continue it instead of
            # starting a second quote box.
            merged = False
            if out and out[-1].lstrip().startswith(">") and out[-1].strip() != ">":
                stats["split_quotes_merged"] += 1
                merged = True

            if not merged:
                if out and out[-1].strip():
                    out.append("")
            for b in body:
                out.append(f"> {b}".rstrip() if b.strip() else ">")
            if not body:
                stats["empty_blockquotes"] += 1
                if merged:
                    pass  # nothing to add
            i = close_idx + 1
            # keep exactly one blank line after the quote
            if i < len(lines) and lines[i].strip():
                out.append("")
            continue

        if P_OPEN.match(line):
            inner, close_idx = _collect_html_block(lines, i, P_CLOSE)
            if close_idx >= len(lines):  # unclosed (table leftovers): leave alone
                out.append(line)
                i += 1
                continue
            body = _quote_text(inner)
            stats["paragraphs"] += 1
            if out and out[-1].strip():
                out.append("")
            out.extend(body)
            i = close_idx + 1
            if i < len(lines) and lines[i].strip():
                out.append("")
            continue

        out.append(line)
        i += 1

    return "\n".join(out)


def fix_inline_html(text: str, stats: Counter) -> str:
    """Remove leftover inline HTML that Markdown readers show as literal tags."""
    if ESCAPED_TAG.search(text):
        stats["escaped_tags"] += len(ESCAPED_TAG.findall(text))
        text = ESCAPED_TAG.sub(r"<\1>", text)
        text = text.replace('href="\\#', 'href="#')

    def splice(match: re.Match) -> str:
        stats["split_words_rejoined"] += 1
        # Keep the original spacing: a word torn mid-way must rejoin with none.
        return match.group(1) + " ".join(match.group(2).split())

    text = INLINE_P_SPLIT.sub(splice, text)

    def to_quote(match: re.Match) -> str:
        stats["inline_blockquotes"] += 1
        body = "\n".join(f"> {l.strip()}" for l in match.group(1).split("\n") if l.strip())
        return f"\n\n{body}\n\n"

    text = INLINE_BLOCKQUOTE.sub(to_quote, text)

    def to_heading(match: re.Match) -> str:
        stats["headings"] += 1
        inner = ANCHOR.sub(r"\1", match.group(2))
        inner = " ".join(UNWRAP.sub("", inner).split())
        return f"\n\n{'#' * int(match.group(1))} {inner}\n\n"

    text = HEADING_TAG.sub(to_heading, text)

    for pattern, marker in EMPHASIS:
        def wrap(match: re.Match, marker: str = marker) -> str:
            lead, inner, trail = match.groups()
            if not inner.strip():
                return lead + trail
            stats["emphasis"] += 1
            # Markdown cannot carry padding inside the markers, so whitespace the
            # tag wrapped moves outside them instead of being dropped.
            lead = " " if lead else ""
            trail = " " if trail else ""
            return f"{lead}{marker}{inner}{marker}{trail}"

        text = pattern.sub(wrap, text)

    if LINE_BREAK.search(text):
        stats["line_breaks"] += len(LINE_BREAK.findall(text))
        text = LINE_BREAK.sub("\n", text)

    def keep_link(match: re.Match) -> str:
        stats["links"] += 1
        return f"[{' '.join(match.group(2).split())}]({match.group(1)})"

    text = LINKED_ANCHOR.sub(keep_link, text)

    if UNWRAP.search(text):
        stats["unwrapped_tags"] += len(UNWRAP.findall(text))
        text = UNWRAP.sub("", text)

    # A few sources open <p dir="rtl"> twice for one paragraph, which leaves an
    # unmatched closer behind. Such a tag sits alone on its line and carries no
    # text, so it is simply dropped.
    orphans = ORPHAN_P_LINE.findall(text)
    if orphans:
        stats["orphan_tags"] += len(orphans)
        text = ORPHAN_P_LINE.sub("", text)

    return text


def fix_footnote_definitions(text: str, stats: Counter) -> str:
    """Add the missing colon to footnote definitions — but only where the reader
    will still show the text.

    A footnote-aware renderer lifts ``[^n]: …`` definitions out of the body and
    prints them only where a matching ``[^n]`` reference appears. Many books put
    every note in a standalone ``Notes`` chapter that contains no references at
    all; there the definitions are the chapter's visible prose, and adding the
    colon would blank the page. So a colon is added only when

    * the file reads like a per-chapter footnote section — at least half of its
      candidate definitions are referenced from inside this same file — and
    * this particular definition is one of the referenced ones.

    Everything else is left as plain text, which is what it renders as today.
    """
    lines = text.split("\n")
    candidates = []
    for i, line in enumerate(lines):
        match = FOOTNOTE_LINE.match(line)
        if match and not match.group(2) and (match.group(3) or "").strip():
            candidates.append((i, match.group(1)))
    if not candidates:
        return text

    referenced = set(INLINE_REFERENCE.findall(text))
    linked = [(i, n) for i, n in candidates if n in referenced]
    if len(linked) < len(candidates) * LOCAL_REFERENCE_THRESHOLD:
        stats["footnote_sections_left_as_prose"] += 1
        return text

    # Many books number their notes per printed page, so one file can define
    # [^1] several times over. Footnote renderers keep the first definition of a
    # number and drop the rest, which would silently delete citations and point
    # the surviving references at the wrong source. Leave such files as prose.
    numbering = Counter(n for _, n in candidates)
    if any(count > 1 for count in numbering.values()):
        stats["footnote_sections_with_repeated_numbers"] += 1
        return text

    for i, number in linked:
        rest = FOOTNOTE_LINE.match(lines[i]).group(3) or ""
        lines[i] = f"[^{number}]:{rest}"
        stats["footnote_defs"] += 1

    return "\n".join(lines)


def normalize(text: str, stats: Counter) -> str:
    text, tables = _protect_tables(text)
    text = fix_entities(text, stats)
    text = fix_page_refs(text, stats)
    text = fix_html_blocks(text, stats)
    text = fix_inline_html(text, stats)
    text = fix_footnote_definitions(text, stats)
    text = re.sub(r"^[ \t]+$", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = _restore_tables(text, tables)
    if not text.endswith("\n"):
        text += "\n"
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["books"])
    parser.add_argument("--check", action="store_true", help="report without writing")
    args = parser.parse_args()

    files: list[str] = []
    for path in args.paths or ["books"]:
        if os.path.isdir(path):
            files.extend(glob.glob(os.path.join(path, "**", "*.md"), recursive=True))
        else:
            files.append(path)

    stats = Counter()
    changed = 0
    for path in sorted(files):
        with open(path, encoding="utf-8") as handle:
            original = handle.read()
        updated = normalize(original, stats)
        if updated != original:
            changed += 1
            if not args.check:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(updated)

    print(f"files scanned : {len(files)}")
    print(f"files changed : {changed}")
    for key in sorted(stats):
        print(f"  {key:<22} {stats[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
