"""Quick-entry syntax: one line of text becomes notes, tags and metrics.

    gold=32 hp=71 #bad-roll #comp:fast9 should have slammed the item here

* ``key=number``  -> metric   (gold=32, hp=71, level=7)
* ``#name``       -> tag      (#bad-roll, #comp:fast9)
* everything else -> note text (original word order, whitespace collapsed)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

METRIC_RE = re.compile(r"^([A-Za-z_][\w\-.]*)=([+-]?\d+(?:\.\d+)?)$")
TAG_RE = re.compile(r"^#(\w[\w\-:/.]*)$")

#: Spellings that should land on the same metric so analysis can rely on one key.
METRIC_ALIASES = {"lvl": "level", "exp": "xp"}


@dataclass
class ParsedEntry:
    tags: list[str] = field(default_factory=list)
    metrics: list[tuple[str, float]] = field(default_factory=list)
    note: str = ""

    @property
    def empty(self) -> bool:
        return not (self.tags or self.metrics or self.note)


def _split_compound(token: str) -> list[str]:
    """Split ``gold=2,hp=100`` into two tokens, but leave prose commas alone.

    A token is only split when every comma-separated piece is itself a metric or
    tag, so ``rolled early, should slam`` keeps its punctuation.
    """
    if "," not in token:
        return [token]
    pieces = [piece for piece in token.split(",") if piece]
    if not pieces:
        return []          # a lone "," is just a separator
    if all(METRIC_RE.match(piece) or TAG_RE.match(piece) for piece in pieces):
        return pieces
    return [token]


def parse_entry(text: str) -> ParsedEntry:
    parsed = ParsedEntry()
    words: list[str] = []
    for raw in text.split():
        for token in _split_compound(raw):
            metric = METRIC_RE.match(token)
            if metric:
                key = metric.group(1).lower()
                parsed.metrics.append((METRIC_ALIASES.get(key, key), float(metric.group(2))))
                continue
            tag = TAG_RE.match(token)
            if tag:
                name = tag.group(1).lower()
                if name not in parsed.tags:
                    parsed.tags.append(name)
                continue
            words.append(token)
    parsed.note = " ".join(words)
    return parsed
