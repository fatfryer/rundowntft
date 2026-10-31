"""Generic analysis over notes, tags and metrics.

Everything is built on one flat table with a row per round event:

    {"game_id", "game_title", "event_id", "label", "stage", "round", "round_type",
     "time_ms", "metrics": {"gold": 32.0, "game:place": 3.0, ...},
     "tags": {...}, "game_tags": {...}, "notes": [...]}

Game-level metrics are copied onto each row with a ``game:`` prefix, so "gold at 4-1
vs final placement" is just ``correlate(rows, "gold", "game:place")``. Gold is not
special: any metric or tag you record can be summarised, compared and correlated the
same way.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from .models import METRIC, NOTE, ROUND, TAG
from .rounds import parse_label
from .store import Store, normalize_key

Row = dict[str, Any]


@dataclass
class Stats:
    n: int
    mean: float
    median: float
    min: float
    max: float
    stdev: Optional[float]


@dataclass
class Comparison:
    metric: str
    tag: str
    scope: str
    with_tag: Optional[Stats]
    without_tag: Optional[Stats]

    @property
    def diff(self) -> Optional[float]:
        """Mean with the tag minus mean without it."""
        if self.with_tag is None or self.without_tag is None:
            return None
        return self.with_tag.mean - self.without_tag.mean


def describe(values: Iterable[Optional[float]]) -> Optional[Stats]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return Stats(
        len(vals), statistics.fmean(vals), statistics.median(vals), min(vals), max(vals),
        statistics.stdev(vals) if len(vals) > 1 else None,
    )


def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) != len(ys):
        raise ValueError("length mismatch")
    n = len(xs)
    if n < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def label_sort_key(label: str) -> tuple:
    parsed = parse_label(label)
    return (0, parsed[0], parsed[1], label) if parsed else (1, 0, 0, label)


# ------------------------------------------------------------------- tables
def event_table(store: Store, game_ids: Optional[Iterable[int]] = None, kind: Optional[str] = ROUND,
                inherit_game: bool = True) -> list[Row]:
    """One row per event (rounds by default), ordered by game then time."""
    if game_ids is None:
        games = store.list_games()
    else:
        games = [g for g in (store.get_game(i) for i in game_ids) if g is not None]
    rows: list[Row] = []
    for game in games:
        game_entries = store.list_entries(game_id=game.id, scope="game")
        game_tags = {e.key for e in game_entries if e.kind == TAG}
        game_metrics = {
            f"game:{e.key}": e.num for e in game_entries if e.kind == METRIC and e.num is not None
        }
        by_event: dict[int, list] = defaultdict(list)
        for entry in store.list_entries(game_id=game.id, scope="event"):
            by_event[entry.event_id].append(entry)  # type: ignore[index]
        for event in store.list_events(game.id):
            if kind is not None and event.kind != kind:
                continue
            entries = by_event.get(event.id, [])
            parsed = parse_label(event.label)
            metrics: dict[str, float] = {}
            for e in entries:
                if e.kind == METRIC and e.num is not None:
                    metrics[e.key] = e.num
            if inherit_game:
                metrics.update(game_metrics)
            rows.append({
                "game_id": game.id,
                "game_title": game.title,
                "event_id": event.id,
                "label": event.label,
                "stage": parsed[0] if parsed else None,
                "round": parsed[1] if parsed else None,
                "round_type": event.round_type,
                "time_ms": event.time_ms,
                "metrics": metrics,
                "tags": {e.key for e in entries if e.kind == TAG},
                "game_tags": set(game_tags),
                "notes": [e.text for e in entries if e.kind == NOTE and e.text],
            })
    return rows


def game_table(store: Store, game_ids: Optional[Iterable[int]] = None) -> list[Row]:
    """One row per game with its game-level metrics, tags and notes."""
    wanted = set(game_ids) if game_ids is not None else None
    rows = []
    for game in store.list_games():
        if wanted is not None and game.id not in wanted:
            continue
        entries = store.list_entries(game_id=game.id, scope="game")
        rows.append({
            "game_id": game.id,
            "game_title": game.title,
            "video_path": game.video_path,
            "metrics": {e.key: e.num for e in entries if e.kind == METRIC and e.num is not None},
            "tags": {e.key for e in entries if e.kind == TAG},
            "notes": [e.text for e in entries if e.kind == NOTE and e.text],
        })
    return rows


def with_deltas(rows: list[Row], key: str, name: Optional[str] = None) -> list[Row]:
    """Add ``<key>_delta``: change since the previous row *in the same game* that has ``key``.

    With gold sampled at each round start, the delta on 3-2 is "gold at 3-2 minus gold at
    3-1", i.e. the net change over the round before it (income minus spending).
    """
    name = name or f"{key}_delta"
    last: dict[int, float] = {}
    for row in sorted(rows, key=lambda r: (r["game_id"], r["time_ms"])):
        value = row["metrics"].get(key)
        if value is None:
            continue
        prev = last.get(row["game_id"])
        if prev is not None:
            row["metrics"][name] = value - prev
        last[row["game_id"]] = value
    return rows


def filter_rows(rows: Iterable[Row], *, labels: Optional[Iterable[str]] = None,
                round_types: Optional[Iterable[str]] = None, tags_all: Iterable[str] = (),
                tags_none: Iterable[str] = (), game_ids: Optional[Iterable[int]] = None,
                scope: str = "event") -> list[Row]:
    label_set = set(labels) if labels is not None else None
    type_set = set(round_types) if round_types is not None else None
    game_set = set(game_ids) if game_ids is not None else None
    need = [normalize_key(t) for t in tags_all]
    ban = [normalize_key(t) for t in tags_none]
    out = []
    for row in rows:
        if label_set is not None and row["label"] not in label_set:
            continue
        if type_set is not None and row["round_type"] not in type_set:
            continue
        if game_set is not None and row["game_id"] not in game_set:
            continue
        if not all(_has_tag(row, t, scope) for t in need):
            continue
        if any(_has_tag(row, t, scope) for t in ban):
            continue
        out.append(row)
    return out


def _has_tag(row: Row, tag: str, scope: str) -> bool:
    if scope == "event":
        return tag in row["tags"]
    if scope == "game":
        return tag in row["game_tags"]
    if scope == "any":
        return tag in row["tags"] or tag in row["game_tags"]
    raise ValueError(f"bad scope {scope!r}")


# ----------------------------------------------------------------- analyses
def metric_by_label(rows: Iterable[Row], key: str) -> list[tuple[str, Stats]]:
    """Stats for ``key`` at each round label (e.g. gold at 3-2 across all games), in round order."""
    key = normalize_key(key)
    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row["metrics"].get(key)
        if value is not None:
            buckets[row["label"]].append(value)
    return [(label, describe(buckets[label])) for label in sorted(buckets, key=label_sort_key)]  # type: ignore[misc]


def compare_by_tag(rows: Iterable[Row], metric: str, tag: str, scope: str = "event") -> Comparison:
    """Metric stats for rows with a tag versus rows without it."""
    metric, tag = normalize_key(metric), normalize_key(tag)
    with_vals, without_vals = [], []
    for row in rows:
        value = row["metrics"].get(metric)
        if value is None:
            continue
        (with_vals if _has_tag(row, tag, scope) else without_vals).append(value)
    return Comparison(metric, tag, scope, describe(with_vals), describe(without_vals))


def correlate(rows: Iterable[Row], key_a: str, key_b: str) -> tuple[Optional[float], int]:
    """Pearson r between two metrics over rows that have both. Returns ``(r, n)``."""
    a, b = normalize_key(key_a), normalize_key(key_b)
    xs, ys = [], []
    for row in rows:
        x, y = row["metrics"].get(a), row["metrics"].get(b)
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
    return pearson(xs, ys), len(xs)


def tag_counts(store: Store, game_ids: Optional[Iterable[int]] = None) -> dict[str, Counter]:
    """How often each tag is used, split into round-level and game-level."""
    wanted = set(game_ids) if game_ids is not None else None
    out: dict[str, Counter] = {"event": Counter(), "game": Counter()}
    for entry in store.list_entries(kind=TAG):
        if wanted is not None and entry.game_id not in wanted:
            continue
        out["game" if entry.event_id is None else "event"][entry.key] += 1
    return out
