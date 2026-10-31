"""CSV export and the portable JSON sidecar.

The SQLite file is the working library. The sidecar (``<video>.tftreview.json``) is a
human-readable copy of one game's review that sits next to the VOD, so it can be
backed up, shared with a coach, or re-imported on another machine.
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
from typing import Any, Iterable, Optional, TextIO

from .analysis import Row
from .models import Game
from .store import Store
from .timeutil import format_time

SIDECAR_SUFFIX = ".tftreview.json"
SIDECAR_FORMAT = "tftreview-sidecar"
SIDECAR_VERSION = 1


# --------------------------------------------------------------------- CSV
def _num(value: Optional[float]) -> str:
    return "" if value is None else f"{value:g}"


def rows_to_csv(rows: Iterable[Row], fp: TextIO, metric_keys: Optional[Iterable[str]] = None) -> int:
    """Write an event table (see ``analysis.event_table``). Returns the number of rows written."""
    rows = list(rows)
    keys = sorted({k for r in rows for k in r["metrics"]}) if metric_keys is None else list(metric_keys)
    writer = csv.writer(fp)
    writer.writerow([
        "game_id", "game", "label", "stage", "round", "round_type", "time_ms", "time",
        "tags", "game_tags", "notes", *keys,
    ])
    for r in rows:
        writer.writerow([
            r["game_id"], r["game_title"], r["label"], r["stage"] if r["stage"] is not None else "",
            r["round"] if r["round"] is not None else "", r["round_type"] or "", r["time_ms"],
            format_time(r["time_ms"]), ";".join(sorted(r["tags"])), ";".join(sorted(r["game_tags"])),
            " | ".join(r["notes"]), *[_num(r["metrics"].get(k)) for k in keys],
        ])
    return len(rows)


def entries_to_csv(store: Store, fp: TextIO, game_ids: Optional[Iterable[int]] = None) -> int:
    """Long-form dump: one line per note / tag / metric, with its game and event context."""
    wanted = set(game_ids) if game_ids is not None else None
    writer = csv.writer(fp)
    writer.writerow(["game_id", "game", "event_label", "event_kind", "scope", "kind", "key", "num", "text", "time_ms"])
    count = 0
    for game in store.list_games():
        if wanted is not None and game.id not in wanted:
            continue
        events = {e.id: e for e in store.list_events(game.id)}
        for entry in store.list_entries(game_id=game.id):
            event = events.get(entry.event_id) if entry.event_id is not None else None
            writer.writerow([
                game.id, game.title, event.label if event else "", event.kind if event else "",
                "event" if event else "game", entry.kind, entry.key, _num(entry.num),
                entry.text or "", entry.time_ms if entry.time_ms is not None else "",
            ])
            count += 1
    return count


# ----------------------------------------------------------------- sidecar
def sidecar_path(video_path: str | os.PathLike) -> str:
    return str(video_path) + SIDECAR_SUFFIX


def game_to_dict(store: Store, game_id: int) -> dict[str, Any]:
    game = store.get_game(game_id)
    if game is None:
        raise KeyError(f"no game {game_id}")
    events = store.list_events(game_id)
    index = {e.id: i for i, e in enumerate(events)}
    return {
        "format": SIDECAR_FORMAT,
        "version": SIDECAR_VERSION,
        "title": game.title,
        "video_name": os.path.basename(game.video_path),
        "meta": game.meta,
        "events": [
            {"kind": e.kind, "label": e.label, "time_ms": e.time_ms, "source": e.source, "meta": e.meta}
            for e in events
        ],
        "entries": [
            {
                "kind": en.kind, "key": en.key, "num": en.num, "text": en.text, "time_ms": en.time_ms,
                "event": index.get(en.event_id) if en.event_id is not None else None,
            }
            for en in store.list_entries(game_id=game_id)
        ],
    }


def write_sidecar(store: Store, game_id: int, path: Optional[str | os.PathLike] = None) -> str:
    """Write the game's sidecar atomically (temp file + rename). Defaults to next to the video."""
    game = store.get_game(game_id)
    if game is None:
        raise KeyError(f"no game {game_id}")
    target = str(path) if path is not None else sidecar_path(game.video_path)
    data = json.dumps(game_to_dict(store, game_id), indent=2, ensure_ascii=False)
    directory = os.path.dirname(os.path.abspath(target))
    fd, tmp = tempfile.mkstemp(prefix=".tftreview-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target


def read_sidecar(path: str | os.PathLike) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or data.get("format") != SIDECAR_FORMAT:
        raise ValueError(f"{path} is not a tftreview sidecar file")
    if data.get("version", 0) > SIDECAR_VERSION:
        raise ValueError(f"{path} was written by a newer version of tftreview")
    return data


def import_game_dict(store: Store, data: dict[str, Any], video_path: str | os.PathLike,
                     replace: bool = False) -> Game:
    """Create (or, with ``replace``, overwrite) the game for ``video_path`` from sidecar data."""
    existing = store.get_game_by_path(video_path)
    if existing is not None and not replace:
        raise ValueError("a game for this video already exists (use replace=True to overwrite it)")
    game = store.add_game(video_path, title=data.get("title"), meta=data.get("meta") or {})
    if existing is not None:
        store.clear_game(game.id)
        game = store.update_game(game.id, title=data.get("title") or game.title, meta=data.get("meta") or {})
    ids = []
    for ev in data.get("events", []):
        ids.append(store.add_event(
            game.id, ev["kind"], ev["label"], int(ev["time_ms"]),
            source=ev.get("source", "manual"), meta=ev.get("meta") or {},
        ).id)
    for en in data.get("entries", []):
        ref = en.get("event")
        store.insert_entry(
            game.id, en["kind"], en["key"], event_id=ids[ref] if ref is not None else None,
            num=en.get("num"), text=en.get("text"), time_ms=en.get("time_ms"),
        )
    return game


def open_game(store: Store, video_path: str | os.PathLike) -> Game:
    """Game for a video: from the library, else from its sidecar, else brand new."""
    game = store.get_game_by_path(video_path)
    if game is not None:
        return game
    sidecar = sidecar_path(video_path)
    if os.path.exists(sidecar):
        try:
            return import_game_dict(store, read_sidecar(sidecar), video_path)
        except (ValueError, KeyError, OSError):
            pass  # unreadable sidecar: start fresh rather than refusing to open the video
    return store.add_game(video_path)
