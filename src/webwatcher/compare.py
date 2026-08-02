"""Content normalisation, hashing and human readable diffs."""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass

# `\s` covers spaces, tabs and NBSP; zero-width characters and the BOM are not
# whitespace to Python, so they get stripped separately before collapsing.
_WHITESPACE = re.compile(r"\s+")
_INVISIBLE = dict.fromkeys((0x200B, 0x200C, 0x200D, 0xFEFF), None)
_MAX_LINE_CHARS = 300


def normalize(text: str, ignore_patterns: list[re.Pattern[str]] | None = None) -> list[str]:
    """Turn raw page text into stable, comparable lines.

    Collapses runs of whitespace, drops empty lines and removes lines matching any
    ignore pattern (timestamps, counters, session ids, ...).
    """
    patterns = ignore_patterns or []
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = _WHITESPACE.sub(" ", raw_line.translate(_INVISIBLE)).strip()
        if not line:
            continue
        if any(pattern.search(line) for pattern in patterns):
            continue
        lines.append(line)
    return lines


def content_hash(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


@dataclass
class Diff:
    added: int
    removed: int
    text: str
    truncated: bool

    @property
    def total(self) -> int:
        return self.added + self.removed

    @property
    def changed(self) -> bool:
        return self.total > 0

    def summary(self) -> str:
        return f"+{self.added} / -{self.removed} Zeilen"


def _shorten(line: str) -> str:
    if len(line) <= _MAX_LINE_CHARS:
        return line
    return line[: _MAX_LINE_CHARS - 1] + "…"


def make_diff(old: list[str], new: list[str], max_lines: int = 60) -> Diff:
    """Build a compact +/- diff, without hunk headers or context noise."""
    added = 0
    removed = 0
    rendered: list[str] = []

    for line in difflib.unified_diff(old, new, lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
        else:
            continue
        rendered.append(f"{line[0]} {_shorten(line[1:].strip())}")

    truncated = len(rendered) > max_lines
    if truncated:
        hidden = len(rendered) - max_lines
        rendered = rendered[:max_lines] + [f"… {hidden} weitere Änderungen"]

    return Diff(added=added, removed=removed, text="\n".join(rendered), truncated=truncated)
