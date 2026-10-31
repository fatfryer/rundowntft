#!/usr/bin/env python
"""Rebuild ``src/tftreview/vision/glyph_templates.npz`` from a reviewed game.

The HUD font is fixed, so the glyph templates only have to be learned once per
resolution/UI scale. This script learns them from a game whose start-of-round metrics the
reviewer already typed in: every reference value says what the digits are, so each
segmented glyph can be labelled and the digit bitmaps rebuilt without any manual work.

Only **level and HP** are used to label the 16 px font. Gold is deliberately left out:
a reference gold value can be a few seconds ahead of or behind the frame it is attached
to (income lands after the round icon flips), so labelling glyphs from it would teach the
templates wrong shapes. Level and HP are stable at the time they are entered.

Usage::

    python scripts/build_glyph_templates.py                 # learns from ./1.mp4
    python scripts/build_glyph_templates.py --video other.mp4 --db path/to/library.sqlite3
    python scripts/build_glyph_templates.py --dry-run       # report only, write nothing
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tftreview import paths                                   # noqa: E402
from tftreview.models import METRIC                           # noqa: E402
from tftreview.store import Store                             # noqa: E402
from tftreview.vision import glyphs                           # noqa: E402
from tftreview.vision.glyphs import FONT12, FONT16            # noqa: E402
from tftreview.vision.metrics import MetricsReader            # noqa: E402
from tftreview.vision.video import frame_at, probe            # noqa: E402

#: XP needed to reach the next level. Learned from the reviewed VOD: level 8 needs 68.
XP_FOR_NEXT = {2: 2, 3: 6, 4: 10, 5: 20, 6: 36, 7: 56, 8: 68, 9: 84, 10: 94}

#: carousel rounds have no readable HUD at the sampled moment
SKIP_ROUNDS = {"1-4", "2-4", "3-4", "4-4", "5-4"}


def reference_rounds(store: Store, game_id: int):
    """Yield ``(label, time_ms, values)`` for rounds that have reference metrics."""
    events = {e.id: e for e in store.list_events(game_id)}
    values_by_event: dict[int, dict[str, float]] = {}
    time_by_event: dict[int, int] = {}
    for entry in store.list_entries(game_id=game_id, kind=METRIC, scope="event"):
        values_by_event.setdefault(entry.event_id, {})[entry.key] = entry.num
        if entry.time_ms is not None:
            # the entry timestamp is when the reviewer read the HUD
            time_by_event[entry.event_id] = entry.time_ms
    for event_id, values in values_by_event.items():
        event = events.get(event_id)
        if event is None or event_id not in time_by_event:
            continue
        wanted = {"gold", "level", "xp", "hp"}
        if not wanted <= values.keys():
            continue
        yield event.label, int(time_by_event[event_id]), values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--video", default="1.mp4", help="reviewed video (default: 1.mp4)")
    parser.add_argument("--db", default=str(paths.db_path()), help="library database")
    parser.add_argument("--out", default=str(glyphs.TEMPLATE_PATH), help="where to write")
    parser.add_argument("--dry-run", action="store_true", help="report but do not write")
    args = parser.parse_args(argv)

    video = str(Path(args.video).expanduser())
    with Store(args.db) as store:
        game = store.get_game_by_path(video)
        if game is None:
            print(f"error: {video} is not in {args.db}", file=sys.stderr)
            return 2
        rounds = list(reference_rounds(store, game.id))

    if not rounds:
        print(f"error: game {game.id} has no rounds with gold+level+xp+hp", file=sys.stderr)
        return 2

    meta = probe(video)
    reader = MetricsReader.for_meta(meta)
    examples: list[tuple[glyphs.Glyph, str, glyphs.FontSpec]] = []
    for label, time_ms, values in rounds:
        if label in SKIP_ROUNDS:
            print(f"skip {label:4} carousel")
            continue
        frame = frame_at(video, meta.frame_of_ms(time_ms), meta)
        values = dict(values, xp_next=XP_FOR_NEXT.get(int(values["level"])))
        found, skipped = reader.label_examples(frame, values, widgets=("level", "hp", "xp"))
        examples.extend(found)
        if skipped:
            print(f"skip {label:4} " + "; ".join(skipped))

    if not examples:
        print("error: no labelled glyphs", file=sys.stderr)
        return 1

    templates = glyphs.templates_from_examples(examples)
    for font, by_digit in sorted(templates.items()):
        counts = Counter(d for _, d, spec in examples if spec.name == font)
        print(f"{font}: {len(by_digit)} digits {glyphs.digit_count(by_digit)} "
              f"({len(examples)} examples)")
        print("   " + "  ".join(f"{d}:{counts[d]}" for d in sorted(counts)))

    missing = set("0123456789") - set(templates.get("font16", {}))
    if missing:
        print(f"warning: font16 has no template for {''.join(sorted(missing))}",
              file=sys.stderr)

    if args.dry_run:
        print("dry run: nothing written")
        return 0
    path = glyphs.save_templates(args.out, templates)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
