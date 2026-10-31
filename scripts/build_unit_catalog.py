#!/usr/bin/env python3
"""Build unit catalog from shop slots across multiple games.

Scans the shop during each round's planning phase, extracts unit names and costs,
and emits a CSV catalog of unique units.

Usage:
    python scripts/build_unit_catalog.py --games 1,2,3,4,5 --output units.csv
    python scripts/build_unit_catalog.py --all --output units.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tftreview.store import Store
from tftreview.vision import detect
from tftreview.vision.shop import ShopModel, ShopReader, iter_shop_frames, UnitInSlot
from tftreview.vision.video import probe


@dataclass
class CatalogUnit:
    name: str
    cost: int
    traits: tuple[str, ...]
    set_name: str
    is_charm: bool
    first_seen_game: int
    first_seen_round: str
    sightings: int = 1

    def key(self) -> tuple:
        return (self.name.lower(), self.cost, self.set_name)

    def as_row(self) -> list[str]:
        return [
            self.name,
            str(self.cost),
            ",".join(self.traits),
            self.set_name,
            "1" if self.is_charm else "0",
            str(self.first_seen_game),
            self.first_seen_round,
            str(self.sightings),
        ]


def parse_game_ids(s: str) -> list[int]:
    """Parse comma-separated game IDs."""
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Build unit catalog from shop slots")
    ap.add_argument("--db", help="Database path (default: app data)")
    ap.add_argument("--games", help="Comma-separated game IDs to scan")
    ap.add_argument("--all", action="store_true", help="Scan all games in database")
    ap.add_argument("--output", default="units.csv", help="Output CSV path")
    ap.add_argument("--set", default="Unknown", help="Set name for catalog (e.g. Set12)")
    ap.add_argument("--step", type=int, default=30, help="Frame sampling step (default 30 = 0.5s at 60fps)")
    ap.add_argument("--min-score", type=float, default=0.7, help="Minimum glyph match score")
    ap.add_argument("--debug-dir", help="Save debug frames to directory")
    ap.add_argument("--max-rounds", type=int, default=10, help="Max rounds per game to scan (for testing)")
    args = ap.parse_args()

    if not args.games and not args.all:
        print("error: specify --games or --all", file=sys.stderr)
        return 2

    # Initialize store
    from tftreview import paths
    store = Store(args.db or paths.db_path())

    try:
        # Determine which games to process
        if args.all:
            games = store.list_games()
            game_ids = [g.id for g in games]
        else:
            game_ids = parse_game_ids(args.games)
            games = [store.get_game(gid) for gid in game_ids]
            games = [g for g in games if g is not None]

        if not games:
            print("No games found", file=sys.stderr)
            return 1

        print(f"Scanning {len(games)} game(s) for units...")

        # Load round config
        from tftreview.rounds import load_config_or_default
        config = load_config_or_default(paths.rounds_path())

        # Catalog: key -> CatalogUnit
        catalog: dict[tuple, CatalogUnit] = {}

        debug_dir = Path(args.debug_dir) if args.debug_dir else None
        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)

        for game in games:
            print(f"\nProcessing game {game.id}: {game.title}")
            print(f"  Video: {game.video_path}")

            # Get round events for this game
            events = store.list_events(game.id, kinds=("round",))
            if not events:
                print(f"  No rounds found - run detect first")
                continue

            # Probe video
            try:
                meta = probe(game.video_path)
            except Exception as e:
                print(f"  Could not probe video: {e}")
                continue

            print(f"  Video: {meta.width}x{meta.height}, {meta.fps_float:.1f}fps, {meta.frame_count} frames")

            shop_model = ShopModel.for_meta(meta)
            shop_reader = ShopReader(shop_model)

            # For each round, sample shop frames during planning phase
            # Planning phase is roughly the first 10-15 seconds of each round
            for i, event in enumerate(events):
                if i >= args.max_rounds:
                    break
                round_label = event.label
                round_start = event.time_ms
                next_start = events[i + 1].time_ms if i + 1 < len(events) else round_start + 30000

                # Sample during planning phase (first 12 seconds of round)
                plan_start = round_start
                plan_end = min(round_start + 12000, next_start)

                if plan_end <= plan_start:
                    continue

                print(f"  Round {round_label}: sampling {plan_start}-{plan_end}ms")

                try:
                    # We need the actual frames for debug, so iterate manually
                    from tftreview.vision.video import iter_frames
                    start_frame = max(0, meta.frame_of_ms(plan_start))
                    end_frame = min(meta.frame_count, meta.frame_of_ms(plan_end))
                    if end_frame <= start_frame:
                        continue

                    frame_count = 0
                    for frame_idx, image in iter_frames(game.video_path, meta, start_frame=start_frame, end_frame=end_frame):
                        if (frame_idx - start_frame) % args.step != 0:
                            continue
                        frame_count += 1
                        slots = shop_reader.read_shop(image)

                        # Process each slot
                        for slot_idx, unit in enumerate(slots):
                            if unit.is_empty():
                                continue

                            # Only process if we have a cost (cost is more reliable than name currently)
                            if unit.cost is None:
                                continue

                            # For now, use a placeholder name since OCR isn't working yet
                            # In practice, we'd match the glyphs to known unit names
                            name = unit.name or f"Unknown_{unit.cost}"

                            key = (name.lower(), unit.cost, args.set)
                            if key in catalog:
                                catalog[key].sightings += 1
                            else:
                                catalog[key] = CatalogUnit(
                                    name=name,
                                    cost=unit.cost,
                                    traits=unit.traits,
                                    set_name=args.set,
                                    is_charm=unit.is_charm,
                                    first_seen_game=game.id,
                                    first_seen_round=round_label,
                                )

                            # Debug: save frame
                            if debug_dir and unit.cost is not None:
                                x0, y0, x1, y1 = shop_model.slot_boxes()[slot_idx]
                                slot_img = image[y0:y1, x0:x1]
                                debug_path = debug_dir / f"game{game.id}_round{round_label}_frame{frame_idx}_slot{slot_idx}_cost{unit.cost}.jpg"
                                import cv2
                                cv2.imwrite(str(debug_path), slot_img)

                    if frame_count > 0:
                        print(f"    Sampled {frame_count} frames")

                except Exception as e:
                    print(f"    Error scanning round {round_label}: {e}")
                    import traceback
                    traceback.print_exc()

                except Exception as e:
                    print(f"    Error scanning round {round_label}: {e}")

        # Write catalog
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "cost", "traits", "set", "is_charm", "first_seen_game", "first_seen_round", "sightings"])
            for unit in sorted(catalog.values(), key=lambda u: (u.cost, u.name)):
                writer.writerow(unit.as_row())

        print(f"\nCatalog written to {output_path}")
        print(f"Total unique units: {len(catalog)}")
        for unit in sorted(catalog.values(), key=lambda u: (u.cost, u.name)):
            print(f"  {unit.name} (cost {unit.cost}) - {unit.sightings} sightings")

        return 0

    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())