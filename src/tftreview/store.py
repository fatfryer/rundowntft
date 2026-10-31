"""SQLite persistence: games, events and entries (notes / tags / metrics)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import ENTRY_KINDS, METRIC, NOTE, TAG, Entry, Event, Game

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    video_path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    time_ms INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_game ON events(game_id, time_ms);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('note','tag','metric')),
    key TEXT NOT NULL,
    num REAL,
    text TEXT,
    time_ms INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_game ON entries(game_id);
CREATE INDEX IF NOT EXISTS idx_entries_event ON entries(event_id);
CREATE INDEX IF NOT EXISTS idx_entries_key ON entries(kind, key);

-- Units catalog (populated by build-catalog, editable by user)
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    cost INTEGER NOT NULL,
    traits TEXT NOT NULL DEFAULT '[]',  -- JSON array
    set_name TEXT,
    is_charm BOOLEAN DEFAULT 0,
    first_seen_game INTEGER REFERENCES games(id),
    first_seen_round TEXT,
    sightings INTEGER DEFAULT 1,
    UNIQUE(name, cost, set_name)
);
CREATE INDEX IF NOT EXISTS idx_units_cost ON units(cost);
CREATE INDEX IF NOT EXISTS idx_units_set ON units(set_name);

-- Gold spends per round
CREATE TABLE IF NOT EXISTS spends (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    frame INTEGER,
    time_ms INTEGER,
    kind TEXT NOT NULL,  -- roll, xp, unit, charm, sell
    unit_id INTEGER REFERENCES units(id),
    cost INTEGER,
    gold_before INTEGER,
    gold_after INTEGER,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_spends_game ON spends(game_id);
CREATE INDEX IF NOT EXISTS idx_spends_event ON spends(event_id);
CREATE INDEX IF NOT EXISTS idx_spends_kind ON spends(kind);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_video_path(path: str | os.PathLike) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


def normalize_key(key: str) -> str:
    return key.strip().lstrip("#").lower()


def _game(row: sqlite3.Row) -> Game:
    return Game(row["id"], row["video_path"], row["title"], row["created_at"], json.loads(row["meta"]))


def _event(row: sqlite3.Row) -> Event:
    return Event(
        row["id"], row["game_id"], row["kind"], row["label"], row["time_ms"],
        row["source"], json.loads(row["meta"]),
    )


def _entry(row: sqlite3.Row) -> Entry:
    return Entry(
        row["id"], row["game_id"], row["event_id"], row["kind"], row["key"],
        row["num"], row["text"], row["time_ms"], row["created_at"],
    )


class Store:
    def __init__(self, path: str | os.PathLike = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ games
    def add_game(self, video_path: str | os.PathLike, title: Optional[str] = None,
                 meta: Optional[dict[str, Any]] = None) -> Game:
        """Create a game for this video, or return the existing one."""
        path = normalize_video_path(video_path)
        existing = self.get_game_by_path(path)
        if existing:
            return existing
        title = title or os.path.splitext(os.path.basename(path))[0]
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO games(video_path, title, created_at, meta) VALUES (?,?,?,?)",
                (path, title, _now(), json.dumps(meta or {})),
            )
        return self.get_game(cur.lastrowid)  # type: ignore[arg-type]

    def get_game(self, game_id: int) -> Optional[Game]:
        row = self.conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        return _game(row) if row else None

    def get_game_by_path(self, video_path: str | os.PathLike) -> Optional[Game]:
        row = self.conn.execute(
            "SELECT * FROM games WHERE video_path=?", (normalize_video_path(video_path),)
        ).fetchone()
        return _game(row) if row else None

    def list_games(self) -> list[Game]:
        return [_game(r) for r in self.conn.execute("SELECT * FROM games ORDER BY id")]

    def update_game(self, game_id: int, *, title: Optional[str] = None,
                    meta: Optional[dict[str, Any]] = None) -> Game:
        with self.conn:
            if title is not None:
                self.conn.execute("UPDATE games SET title=? WHERE id=?", (title, game_id))
            if meta is not None:
                self.conn.execute("UPDATE games SET meta=? WHERE id=?", (json.dumps(meta), game_id))
        game = self.get_game(game_id)
        if game is None:
            raise KeyError(f"no game {game_id}")
        return game

    def delete_game(self, game_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM games WHERE id=?", (game_id,))

    def clear_game(self, game_id: int) -> None:
        """Remove every event and entry of a game but keep the game row."""
        with self.conn:
            self.conn.execute("DELETE FROM entries WHERE game_id=?", (game_id,))
            self.conn.execute("DELETE FROM events WHERE game_id=?", (game_id,))

    # ----------------------------------------------------------------- events
    def add_event(self, game_id: int, kind: str, label: str, time_ms: int,
                  source: str = "manual", meta: Optional[dict[str, Any]] = None) -> Event:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO events(game_id, kind, label, time_ms, source, meta) VALUES (?,?,?,?,?,?)",
                (game_id, kind, label, int(time_ms), source, json.dumps(meta or {})),
            )
        return self.get_event(cur.lastrowid)  # type: ignore[arg-type, return-value]

    def get_event(self, event_id: int) -> Optional[Event]:
        row = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return _event(row) if row else None

    def list_events(self, game_id: int, kinds: Optional[Iterable[str]] = None) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE game_id=? ORDER BY time_ms, id", (game_id,)
        ).fetchall()
        events = [_event(r) for r in rows]
        if kinds is not None:
            wanted = set(kinds)
            events = [e for e in events if e.kind in wanted]
        return events

    def find_event(self, game_id: int, kind: str, label: str) -> Optional[Event]:
        row = self.conn.execute(
            "SELECT * FROM events WHERE game_id=? AND kind=? AND label=? ORDER BY id LIMIT 1",
            (game_id, kind, label),
        ).fetchone()
        return _event(row) if row else None

    def update_event(self, event_id: int, *, label: Optional[str] = None,
                     time_ms: Optional[int] = None, source: Optional[str] = None,
                     meta: Optional[dict[str, Any]] = None) -> Event:
        with self.conn:
            if label is not None:
                self.conn.execute("UPDATE events SET label=? WHERE id=?", (label, event_id))
            if time_ms is not None:
                self.conn.execute("UPDATE events SET time_ms=? WHERE id=?", (int(time_ms), event_id))
            if source is not None:
                self.conn.execute("UPDATE events SET source=? WHERE id=?", (source, event_id))
            if meta is not None:
                self.conn.execute("UPDATE events SET meta=? WHERE id=?", (json.dumps(meta), event_id))
        event = self.get_event(event_id)
        if event is None:
            raise KeyError(f"no event {event_id}")
        return event

    def delete_event(self, event_id: int) -> None:
        """Delete an event. Entries attached to it are deleted with it (FK cascade)."""
        with self.conn:
            self.conn.execute("DELETE FROM events WHERE id=?", (event_id,))

    # ---------------------------------------------------------------- entries
    def insert_entry(self, game_id: int, kind: str, key: str, *, event_id: Optional[int] = None,
                     num: Optional[float] = None, text: Optional[str] = None,
                     time_ms: Optional[int] = None, created_at: Optional[str] = None) -> Entry:
        if kind not in ENTRY_KINDS:
            raise ValueError(f"bad entry kind {kind!r}")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO entries(game_id, event_id, kind, key, num, text, time_ms, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (game_id, event_id, kind, key, num, text, time_ms, created_at or _now()),
            )
        return self.get_entry(cur.lastrowid)  # type: ignore[arg-type, return-value]

    def get_entry(self, entry_id: int) -> Optional[Entry]:
        row = self.conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return _entry(row) if row else None

    def add_note(self, game_id: int, text: str, event_id: Optional[int] = None,
                 time_ms: Optional[int] = None) -> Entry:
        return self.insert_entry(game_id, NOTE, "note", event_id=event_id, text=text, time_ms=time_ms)

    def add_tag(self, game_id: int, tag: str, event_id: Optional[int] = None,
                time_ms: Optional[int] = None) -> Entry:
        """Add a tag; adding the same tag to the same target twice is a no-op."""
        key = normalize_key(tag)
        if not key:
            raise ValueError("empty tag")
        row = self.conn.execute(
            "SELECT * FROM entries WHERE game_id=? AND event_id IS ? AND kind='tag' AND key=?",
            (game_id, event_id, key),
        ).fetchone()
        if row:
            return _entry(row)
        return self.insert_entry(game_id, TAG, key, event_id=event_id, time_ms=time_ms)

    def remove_tag(self, game_id: int, tag: str, event_id: Optional[int] = None) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM entries WHERE game_id=? AND event_id IS ? AND kind='tag' AND key=?",
                (game_id, event_id, normalize_key(tag)),
            )
        return cur.rowcount > 0

    def set_metric(self, game_id: int, key: str, value: float, event_id: Optional[int] = None,
                   time_ms: Optional[int] = None) -> Entry:
        """Set a metric on a target. One value per (target, key): setting again overwrites."""
        key = normalize_key(key)
        if not key:
            raise ValueError("empty metric name")
        row = self.conn.execute(
            "SELECT * FROM entries WHERE game_id=? AND event_id IS ? AND kind='metric' AND key=?",
            (game_id, event_id, key),
        ).fetchone()
        if row:
            with self.conn:
                self.conn.execute(
                    "UPDATE entries SET num=?, time_ms=? WHERE id=?", (float(value), time_ms, row["id"])
                )
            return self.get_entry(row["id"])  # type: ignore[return-value]
        return self.insert_entry(game_id, METRIC, key, event_id=event_id, num=float(value), time_ms=time_ms)

    def update_note(self, entry_id: int, text: str) -> Entry:
        with self.conn:
            self.conn.execute("UPDATE entries SET text=? WHERE id=? AND kind='note'", (text, entry_id))
        entry = self.get_entry(entry_id)
        if entry is None:
            raise KeyError(f"no entry {entry_id}")
        return entry

    def delete_entry(self, entry_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))

    def list_entries(self, game_id: Optional[int] = None, event_id: Optional[int] = None,
                     kind: Optional[str] = None, key: Optional[str] = None,
                     scope: Optional[str] = None) -> list[Entry]:
        """Filter entries. ``scope`` is ``"game"`` (game-level only), ``"event"`` (event-level only) or None."""
        where, params = [], []  # type: list[str], list[Any]
        if game_id is not None:
            where.append("game_id=?")
            params.append(game_id)
        if event_id is not None:
            where.append("event_id=?")
            params.append(event_id)
        if kind is not None:
            where.append("kind=?")
            params.append(kind)
        if key is not None:
            where.append("key=?")
            params.append(normalize_key(key))
        if scope == "game":
            where.append("event_id IS NULL")
        elif scope == "event":
            where.append("event_id IS NOT NULL")
        elif scope is not None:
            raise ValueError(f"bad scope {scope!r}")
        sql = "SELECT * FROM entries"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        return [_entry(r) for r in self.conn.execute(sql, params)]

    # ------------------------------------------------------------------ units
    def add_unit(self, name: str, cost: int, traits: list[str] = None,
                 set_name: str = None, is_charm: bool = False,
                 first_seen_game: int = None, first_seen_round: str = None) -> int:
        """Add or update a unit in the catalog. Returns unit ID."""
        import json
        traits = traits or []
        with self.conn:
            # Try to update existing
            row = self.conn.execute(
                "SELECT id, sightings FROM units WHERE name=? AND cost=? AND set_name=?",
                (name, cost, set_name)
            ).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE units SET sightings=sightings+1, traits=?, is_charm=? WHERE id=?",
                    (json.dumps(traits), int(is_charm), row["id"])
                )
                return row["id"]
            # Insert new
            cur = self.conn.execute(
                "INSERT INTO units(name, cost, traits, set_name, is_charm, first_seen_game, first_seen_round, sightings)"
                " VALUES (?,?,?,?,?,?,?,1)",
                (name, cost, json.dumps(traits), set_name, int(is_charm), first_seen_game, first_seen_round)
            )
            return cur.lastrowid

    def get_unit(self, unit_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
        if row:
            import json
            d = dict(row)
            d["traits"] = json.loads(d["traits"])
            return d
        return None

    def list_units(self, cost: int = None, set_name: str = None) -> list[dict]:
        import json
        where, params = [], []
        if cost is not None:
            where.append("cost=?")
            params.append(cost)
        if set_name is not None:
            where.append("set_name=?")
            params.append(set_name)
        sql = "SELECT * FROM units"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY cost, name"
        rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["traits"] = json.loads(d["traits"])
            result.append(d)
        return result

    def update_unit(self, unit_id: int, **kwargs) -> dict:
        import json
        allowed = {"name", "cost", "traits", "set_name", "is_charm", "first_seen_game", "first_seen_round"}
        sets = []
        params = []
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if k == "traits":
                v = json.dumps(v)
            elif k == "is_charm":
                v = int(v)
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return self.get_unit(unit_id)
        params.append(unit_id)
        with self.conn:
            self.conn.execute(f"UPDATE units SET {', '.join(sets)} WHERE id=?", params)
        return self.get_unit(unit_id)

    # ------------------------------------------------------------------ spends
    def add_spend(self, game_id: int, event_id: int, kind: str, cost: int,
                  gold_before: int, gold_after: int, frame: int = None,
                  time_ms: int = None, unit_id: int = None,
                  meta: dict = None) -> int:
        import json
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO spends(game_id, event_id, frame, time_ms, kind, unit_id, cost, gold_before, gold_after, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (game_id, event_id, frame, time_ms, kind, unit_id, cost, gold_before, gold_after, json.dumps(meta or {}))
            )
            return cur.lastrowid

    def list_spends(self, game_id: int = None, event_id: int = None, kind: str = None) -> list[dict]:
        where, params = [], []
        if game_id is not None:
            where.append("game_id=?")
            params.append(game_id)
        if event_id is not None:
            where.append("event_id=?")
            params.append(event_id)
        if kind is not None:
            where.append("kind=?")
            params.append(kind)
        sql = "SELECT * FROM spends"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY time_ms, id"
        import json
        rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["meta"] = json.loads(d["meta"])
            result.append(d)
        return result
