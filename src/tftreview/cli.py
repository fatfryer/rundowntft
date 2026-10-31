"""Command line interface: drives the same core as the UI, handy for scripting and validation."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from . import analysis, paths
from .export import (entries_to_csv, game_to_dict, import_game_dict, open_game, read_sidecar,
                     rows_to_csv, sidecar_path, write_sidecar)
from .keymap import describe, load_keymap
from .models import METRIC, ROUND
from .rounds import load_config_or_default
from .session import NoEventError, ReviewSession, SequenceExhausted
from .store import Store
from .timeutil import format_time, parse_time


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    cells = [[str(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    for r in cells:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def resolve_game(store: Store, ref: str):
    if ref.isdigit():
        game = store.get_game(int(ref))
        if game:
            return game
    game = store.get_game_by_path(ref)
    if game:
        return game
    needle = ref.lower()
    matches = [g for g in store.list_games()
               if needle in g.title.lower() or needle in os.path.basename(g.video_path).lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError(f"no game matches {ref!r} (see `tftreview games`)")
    raise LookupError(f"{ref!r} is ambiguous: " + ", ".join(f"{g.id}:{g.title}" for g in matches))


def _session(store: Store, ref: str) -> ReviewSession:
    return ReviewSession(store, resolve_game(store, ref), load_config_or_default(paths.rounds_path()))


def _fmt(value: Optional[float], digits: int = 2) -> str:
    return "" if value is None else f"{value:.{digits}f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- commands
def cmd_ui(args, store: Store) -> int:
    try:
        from .ui.app import run
    except ImportError:
        print("The UI needs PySide6:  uv sync --extra ui   (or pip install 'rundowntft[ui]')",
              file=sys.stderr)
        return 1
    store.close()
    return run(video=args.video, db=args.db)


def cmd_db(args, store: Store) -> int:
    print(store.path)
    return 0


def cmd_new(args, store: Store) -> int:
    game = open_game(store, args.video)
    if args.title:
        game = store.update_game(game.id, title=args.title)
    print(f"{game.id}\t{game.title}\t{game.video_path}")
    return 0


def cmd_games(args, store: Store) -> int:
    rows = []
    for g in store.list_games():
        rounds = len(store.list_events(g.id, kinds=(ROUND,)))
        rows.append((g.id, g.title, rounds, len(store.list_entries(game_id=g.id)), g.video_path))
    _print_table(["id", "title", "rounds", "entries", "video"], rows)
    return 0


def cmd_events(args, store: Store) -> int:
    session = _session(store, args.game)
    rows = [(e.label, e.kind, e.round_type or "", format_time(e.time_ms), e.source) for e in session.events()]
    _print_table(["label", "kind", "type", "time", "source"], rows)
    nxt = session.next_expected()
    print(f"\nnext expected round: {nxt.label if nxt else '(sequence complete)'}")
    return 0


def cmd_stamp(args, store: Store) -> int:
    session = _session(store, args.game)
    event = session.stamp_label(args.label, parse_time(args.time))
    print(f"marked {event.label} ({event.round_type}) at {format_time(event.time_ms)}")
    return 0


def cmd_stamp_next(args, store: Store) -> int:
    session = _session(store, args.game)
    try:
        event = session.stamp_next(parse_time(args.time))
    except SequenceExhausted as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"marked {event.label} ({event.round_type}) at {format_time(event.time_ms)}")
    return 0


def cmd_marker(args, store: Store) -> int:
    session = _session(store, args.game)
    event = session.add_marker(parse_time(args.time), args.label)
    print(f"marker {event.label!r} at {format_time(event.time_ms)}")
    return 0


def cmd_add(args, store: Store) -> int:
    session = _session(store, args.game)
    text = " ".join(args.text)
    ms = parse_time(args.at) if args.at else None
    event_id = None
    scope = "game" if args.game_level else "event" if args.event or ms is not None else "game"
    if args.event:
        event = store.find_event(session.game.id, ROUND, args.event)
        if event is None:
            print(f"no round {args.event!r} marked in this game", file=sys.stderr)
            return 1
        event_id = event.id
    try:
        result = session.add_entry(text, ms=ms, scope=scope, event_id=event_id)
    except NoEventError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    target = f"round {result.event.label}" if result.event else "game"
    print(f"saved {len(result.created)} entr{'y' if len(result.created) == 1 else 'ies'} on {target}: "
          + ", ".join(e.display() for e in result.created))
    return 0


def cmd_notes(args, store: Store) -> int:
    session = _session(store, args.game)
    events = {e.id: e for e in session.events()}
    rows = []
    for e in store.list_entries(game_id=session.game.id):
        ev = events.get(e.event_id) if e.event_id is not None else None
        rows.append((ev.label if ev else "(game)", e.kind, e.display(), format_time(e.time_ms) if e.time_ms is not None else ""))
    rows.sort(key=lambda r: (r[0] != "(game)", analysis.label_sort_key(r[0])))
    _print_table(["on", "kind", "entry", "at"], rows)
    return 0


def _rows(store: Store, args) -> list[dict]:
    ids = [resolve_game(store, g).id for g in args.game] if args.game else None
    rows = analysis.event_table(store, ids)
    for key in getattr(args, "delta", None) or []:
        analysis.with_deltas(rows, key)
    return rows


def cmd_analyze(args, store: Store) -> int:
    what = args.what
    if what == "tags":
        ids = [resolve_game(store, g).id for g in args.game] if args.game else None
        counts = analysis.tag_counts(store, ids)
        for scope in ("game", "event"):
            print(f"[{scope}-level tags]")
            for tag, n in counts[scope].most_common():
                print(f"  #{tag}  x{n}")
        return 0
    rows = _rows(store, args)
    if what == "metric":
        data = analysis.metric_by_label(rows, args.params[0])
        _print_table(["round", "n", "mean", "median", "min", "max"],
                     [(lab, s.n, _fmt(s.mean), _fmt(s.median), _fmt(s.min), _fmt(s.max)) for lab, s in data])
    elif what == "compare":
        if len(args.params) != 2:
            print("usage: analyze compare METRIC TAG", file=sys.stderr)
            return 2
        c = analysis.compare_by_tag(rows, args.params[0], args.params[1], scope=args.scope)
        out = [("with #" + c.tag, c.with_tag), ("without", c.without_tag)]
        _print_table(["group", "n", "mean", "median", "min", "max"],
                     [(name, s.n, _fmt(s.mean), _fmt(s.median), _fmt(s.min), _fmt(s.max)) if s else (name, 0, "", "", "", "")
                      for name, s in out])
        print(f"\nmean difference: {_fmt(c.diff)}")
    elif what == "corr":
        if len(args.params) != 2:
            print("usage: analyze corr METRIC_A METRIC_B", file=sys.stderr)
            return 2
        r, n = analysis.correlate(rows, args.params[0], args.params[1])
        print(f"pearson r = {_fmt(r, 3) or 'n/a'}  (n={n})")
    elif what == "table":
        keys = sorted({k for r in rows for k in r["metrics"]})
        _print_table(["game", "round", "time", *keys, "tags"],
                     [(r["game_id"], r["label"], format_time(r["time_ms"]),
                       *[_fmt(r["metrics"].get(k)) for k in keys], ",".join(sorted(r["tags"]))) for r in rows])
    return 0


def cmd_detect(args, store: Store) -> int:
    """Scan a video for round-start timestamps; optionally store them."""
    try:
        from .vision import detect as run_detect
        from .vision.video import VideoError
    except ImportError as exc:   # pragma: no cover - numpy/opencv missing
        print(f"round detection needs numpy and opencv: {exc}", file=sys.stderr)
        return 1

    config = load_config_or_default(paths.rounds_path())
    start_ms = parse_time(args.start) if args.start else 0
    end_ms = parse_time(args.end) if args.end else None
    if end_ms is not None and end_ms <= start_ms:
        print("--to must be later than --from", file=sys.stderr)
        return 2

    progress = None
    if not args.quiet and not args.json:
        def progress(done: int, total: int, found: int) -> None:
            print(f"\r  scanning {done * 100 / max(1, total):5.1f}%  "
                  f"{done}/{total} frames  {found} rounds", end="", file=sys.stderr, flush=True)

    try:
        result = run_detect(args.video, config, start_ms=start_ms, end_ms=end_ms,
                            progress=progress, debug_dir=args.debug_dir,
                            confirm_round=args.confirm, first_stage=args.stage,
                            frame_stride=args.frame_stride, threads=args.threads)
    except VideoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if progress:
            print(file=sys.stderr)

    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        if not args.quiet:
            v = result.meta
            print(f"{os.path.basename(args.video)}: {v.width}x{v.height} "
                  f"{v.fps_float:g} fps, {v.frame_count} frames, {format_time(v.duration_s * 1000)}")
        _print_table(["round", "time", "frame", "conf", "type"],
                     [(d.label, format_time(d.time_ms, with_ms=True), d.frame,
                       f"{d.confidence:.2f}", d.round_type) for d in result.events])
        if not result.events and not args.quiet:
            print("(no rounds found)")

    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not args.json and not args.quiet:
        print(f"{len(result.events)} rounds in {result.seconds:.1f}s "
              f"({result.frames_scanned} frames scanned)", file=sys.stderr)

    if not args.write or args.dry_run:
        if not args.json and not args.quiet:
            print("dry run: nothing written (pass --write to store these rounds)", file=sys.stderr)
        return 0

    game = open_game(store, args.video)
    added = updated = skipped = 0
    for det in result.events:
        existing = store.find_event(game.id, ROUND, det.label)
        if existing is not None:
            if not args.force:
                skipped += 1
                continue
            store.update_event(existing.id, time_ms=det.time_ms, source="auto", meta=det.meta())
            updated += 1
        else:
            store.add_event(game.id, ROUND, det.label, det.time_ms, source="auto", meta=det.meta())
            added += 1
    print(f"game {game.id} ({game.title}): {added} added, {updated} updated, {skipped} kept")
    if skipped:
        print("(pass --force to overwrite rounds that were already marked by hand)", file=sys.stderr)
    if args.sidecar:
        print(f"wrote {write_sidecar(store, game.id)}")
    return 0


def cmd_read_metrics(args, store: Store) -> int:
    """Read gold / level / XP / HP from the HUD at every round start."""
    try:
        from .vision.metrics import MetricsReader, check_round
        from .vision.video import VideoError, probe
    except ImportError as exc:   # pragma: no cover - numpy/opencv missing
        print(f"reading metrics needs numpy and opencv: {exc}", file=sys.stderr)
        return 1

    session = _session(store, args.game)
    game = session.game
    rounds = [e for e in session.events() if e.kind == ROUND]
    if not rounds:
        print(f"game {game.id} has no rounds yet; run `tftreview detect --write` first",
              file=sys.stderr)
        return 1
    if args.round:
        known = {e.label for e in rounds}
        missing = [label for label in args.round if label not in known]
        if missing:
            print(f"no such round in game {game.id}: {', '.join(missing)}", file=sys.stderr)
            return 1
        rounds = [e for e in rounds if e.label in set(args.round)]

    try:
        meta = probe(game.video_path)
    except VideoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    reader = MetricsReader.for_meta(meta)

    # every metric already on a round is a reference value typed by the reviewer
    references: dict[int, dict[str, int]] = {}
    existing: dict[tuple[int, str], Any] = {}
    for entry in store.list_entries(game_id=game.id, kind=METRIC, scope="event"):
        references.setdefault(entry.event_id, {})[entry.key] = int(entry.num)
        existing[(entry.event_id, entry.key)] = entry

    all_rounds = [e for e in session.events() if e.kind == ROUND]
    next_by_event = {e.id: (all_rounds[i + 1].time_ms if i + 1 < len(all_rounds) else None)
                     for i, e in enumerate(all_rounds)}

    checks = []
    for i, event in enumerate(rounds):
        if not args.quiet and not args.json:
            print(f"\r  reading {event.label:4} ({i + 1}/{len(rounds)})",
                  end="", file=sys.stderr, flush=True)
        checks.append(check_round(
            game.video_path, event.time_ms, next_by_event.get(event.id), label=event.label,
            reference=references.get(event.id), meta=meta, reader=reader,
            step=args.step, max_wait_s=args.wait, late_wait_s=args.late_wait))
    if not args.quiet and not args.json:
        print(file=sys.stderr)

    if args.json:
        print(json.dumps([c.as_dict() for c in checks], indent=2))
    else:
        def show(value: Optional[int]) -> str:
            return "-" if value is None else str(value)

        rows = []
        verdicts = []
        for event, check in zip(rounds, checks):
            m = check.sample.metrics if check.sample else None
            ref = "/".join(str(check.reference.get(k, "-"))
                           for k in ("gold", "level", "xp", "hp"))
            carousel = session.config.type_for(event.label) == "carousel"
            verdict = "carousel" if carousel else check.verdict
            if not carousel and verdict == "late" and check.late_ms is not None:
                verdict = f"late {format_time(check.late_ms)}"
            elif verdict == "none":
                verdict = "no reading"
            verdicts.append(check.verdict if not carousel else "carousel")
            rows.append((
                check.label,
                format_time(check.sample.time_ms) if check.sample else "-",
                str(check.sample.frame) if check.sample else "-",
                show(m.gold if m else None), show(m.level if m else None),
                show(m.xp if m else None), show(m.xp_next if m else None),
                show(m.hp if m else None),
                ref if check.reference else "-", verdict,
            ))
        _print_table(["round", "at", "frame", "gold", "lvl", "xp", "next", "hp",
                      "ref g/l/x/h", "verdict"], rows)
        counts = {}
        for verdict in verdicts:
            counts[verdict] = counts.get(verdict, 0) + 1
        print("\n" + ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items())))

    if not args.write or args.dry_run:
        if not args.json and not args.quiet:
            print("dry run: nothing written (pass --write to store these values)",
                  file=sys.stderr)
        return 0

    writes: list[tuple[int, str, float, int]] = []
    kept = skipped_carousel = 0
    for event, check in zip(rounds, checks):
        if check.sample is None:
            continue
        if session.config.type_for(event.label) == "carousel":
            skipped_carousel += 1
            continue
        m = check.sample.metrics
        for key in ("gold", "level", "xp", "xp_next", "hp"):
            value = getattr(m, key)
            if value is None:
                continue
            if (event.id, key) in existing and not args.force:
                kept += 1
                continue
            writes.append((event.id, key, float(value), check.sample.time_ms))

    overwritten = [existing[(event_id, key)] for event_id, key, _, _ in writes
                   if (event_id, key) in existing]
    if overwritten:
        snapshot = Path(args.snapshot) if args.snapshot else \
            Path(game.video_path + ".tftreview.refs.json")
        events_by_id = {e.id: e for e in all_rounds}
        payload = {
            "game_id": game.id,
            "title": game.title,
            "video_path": game.video_path,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "reference metrics replaced by `tftreview read-metrics --write --force`",
            "entries": [
                {"round": events_by_id[e.event_id].label if e.event_id in events_by_id else "",
                 "event_id": e.event_id, "key": e.key, "num": e.num, "time_ms": e.time_ms}
                for e in overwritten
            ],
        }
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"saved {len(overwritten)} reference entries to {snapshot}")

    for event_id, key, value, time_ms in writes:
        store.set_metric(game.id, key, value, event_id=event_id, time_ms=time_ms)
    print(f"game {game.id} ({game.title}): {len(writes)} metrics written, {kept} kept"
          + (f", {skipped_carousel} carousel rounds skipped" if skipped_carousel else ""))
    if kept and not args.force:
        print("(pass --force to replace reference values you entered by hand)",
              file=sys.stderr)
    if args.sidecar:
        print(f"wrote {write_sidecar(store, game.id)}")
    return 0


def cmd_export(args, store: Store) -> int:
    ids = [resolve_game(store, g).id for g in args.game] if args.game else None
    if args.what == "sidecar":
        if len(args.game or []) != 1:
            print("export sidecar needs exactly one --game", file=sys.stderr)
            return 2
        out = write_sidecar(store, ids[0], args.path)  # type: ignore[index]
        print(f"wrote {out}")
        return 0
    with open(args.path, "w", newline="", encoding="utf-8") as fh:
        if args.what == "csv":
            rows = analysis.event_table(store, ids)
            for key in args.delta or []:
                analysis.with_deltas(rows, key)
            n = rows_to_csv(rows, fh)
        else:
            n = entries_to_csv(store, fh, ids)
    print(f"wrote {n} rows to {args.path}")
    return 0


def cmd_import(args, store: Store) -> int:
    data = read_sidecar(args.path)
    video = args.video or args.path[: -len(".tftreview.json")] if args.path.endswith(".tftreview.json") else args.video
    if not video:
        print("could not infer the video path; pass --video", file=sys.stderr)
        return 2
    game = import_game_dict(store, data, video, replace=args.replace)
    print(f"imported game {game.id}: {game.title}")
    return 0


def cmd_rounds(args, store: Store) -> int:
    config = load_config_or_default(paths.rounds_path())
    print(json.dumps(config.to_dict(), indent=2))
    print(f"\n(override by saving JSON like this to {paths.rounds_path()})", file=sys.stderr)
    return 0


def cmd_hotkeys(args, store: Store) -> int:
    keymap, warnings = load_keymap(paths.keymap_path())
    print(describe(keymap))
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_build_catalog(args, store: Store) -> int:
    """Scan shop slots during planning phases to build a unit catalog."""
    try:
        from .vision.shop import ShopModel, ShopReader
        from .vision.video import probe
        from .rounds import load_config_or_default
    except ImportError as exc:   # pragma: no cover - numpy/opencv missing
        print(f"building catalog needs numpy and opencv: {exc}", file=sys.stderr)
        return 1

    # Determine which games to process
    if args.all:
        games = store.list_games()
        game_ids = [g.id for g in games]
    elif args.games:
        game_ids = [int(x.strip()) for x in args.games.split(",") if x.strip()]
        games = [store.get_game(gid) for gid in game_ids]
        games = [g for g in games if g is not None]
    else:
        print("error: specify --games or --all", file=sys.stderr)
        return 2

    if not games:
        print("No games found", file=sys.stderr)
        return 1

    print(f"Scanning {len(games)} game(s) for units...")

    config = load_config_or_default(paths.rounds_path())

    # Catalog: key -> (name, cost, traits, is_charm, first_seen_game, first_seen_round, sightings)
    catalog: dict[tuple, dict] = {}

    debug_dir = Path(args.debug_dir) if args.debug_dir else None
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)

    for game in games:
        print(f"\nProcessing game {game.id}: {game.title}")
        print(f"  Video: {game.video_path}")

        events = store.list_events(game.id, kinds=("round",))
        if not events:
            print(f"  No rounds found - run detect first")
            continue

        try:
            meta = probe(game.video_path)
        except Exception as e:
            print(f"  Could not probe video: {e}")
            continue

        print(f"  Video: {meta.width}x{meta.height}, {meta.fps_float:.1f}fps, {meta.frame_count} frames")

        shop_model = ShopModel.for_meta(meta)
        shop_reader = ShopReader(shop_model)

        # For each round, sample shop frames during planning phase
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

            if not args.quiet:
                print(f"  Round {round_label}: sampling {plan_start}-{plan_end}ms")

            try:
                from .vision.video import iter_frames
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

                        if unit.cost is None:
                            continue

                        # For now, use a placeholder name since OCR isn't working yet
                        name = unit.name or f"Unknown_{unit.cost}"

                        key = (name.lower(), unit.cost, args.set)
                        if key in catalog:
                            catalog[key]["sightings"] += 1
                        else:
                            catalog[key] = {
                                "name": name,
                                "cost": unit.cost,
                                "traits": list(unit.traits),
                                "set": args.set,
                                "is_charm": unit.is_charm,
                                "first_seen_game": game.id,
                                "first_seen_round": round_label,
                                "sightings": 1,
                            }

                        # Debug: save frame
                        if debug_dir and unit.cost is not None:
                            x0, y0, x1, y1 = shop_model.slot_boxes()[slot_idx]
                            slot_img = image[y0:y1, x0:x1]
                            debug_path = debug_dir / f"game{game.id}_round{round_label}_frame{frame_idx}_slot{slot_idx}_cost{unit.cost}.jpg"
                            import cv2
                            cv2.imwrite(str(debug_path), slot_img)

                if frame_count > 0 and not args.quiet:
                    print(f"    Sampled {frame_count} frames")

            except Exception as e:
                print(f"    Error scanning round {round_label}: {e}")
                import traceback
                traceback.print_exc()

    # Write catalog
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    import csv
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "cost", "traits", "set", "is_charm", "first_seen_game", "first_seen_round", "sightings"])
        for unit in sorted(catalog.values(), key=lambda u: (u["cost"], u["name"])):
            writer.writerow([
                unit["name"],
                str(unit["cost"]),
                ",".join(unit["traits"]),
                unit["set"],
                "1" if unit["is_charm"] else "0",
                str(unit["first_seen_game"]),
                unit["first_seen_round"],
                str(unit["sightings"]),
            ])

    print(f"\nCatalog written to {output_path}")
    print(f"Total unique units: {len(catalog)}")
    for unit in sorted(catalog.values(), key=lambda u: (u["cost"], u["name"])):
        print(f"  {unit['name']} (cost {unit['cost']}) - {unit['sightings']} sightings")

    return 0


def cmd_scan_spends(args, store: Store) -> int:
    """Scan for gold spend actions during rounds."""
    try:
        from .vision import scan_spends
        from .vision.shop import ShopModel, ShopReader
        from .vision.metrics import MetricsReader
        from .vision.video import probe
        from .rounds import load_config_or_default
    except ImportError as exc:
        print(f"scanning spends needs numpy and opencv: {exc}", file=sys.stderr)
        return 1

    session = _session(store, args.game)
    game = session.game
    rounds = [e for e in session.events() if e.kind == "round"]
    if not rounds:
        print(f"game {game.id} has no rounds yet; run `tftreview detect --write` first", file=sys.stderr)
        return 1

    if args.round:
        known = {e.label for e in rounds}
        missing = [label for label in args.round if label not in known]
        if missing:
            print(f"no such round in game {game.id}: {', '.join(missing)}", file=sys.stderr)
            return 1
        rounds = [e for e in rounds if e.label in set(args.round)]

    try:
        meta = probe(game.video_path)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    shop_model = ShopModel.for_meta(meta)
    shop_reader = ShopReader(shop_model)
    metrics_reader = MetricsReader.for_meta(meta)

    all_spends = []
    all_rounds = [e for e in session.events() if e.kind == "round"]
    next_by_event = {e.id: (all_rounds[i + 1].time_ms if i + 1 < len(all_rounds) else None)
                     for i, e in enumerate(all_rounds)}

    for i, event in enumerate(rounds):
        if not args.quiet:
            print(f"\r  scanning {event.label:4} ({i + 1}/{len(rounds)})", end="", file=sys.stderr, flush=True)

        round_start = event.time_ms
        next_start = next_by_event.get(event.id)

        try:
            results = scan_spends(
                game.video_path,
                round_start,
                next_start if next_start else round_start + 30000,
                shop_reader=shop_reader,
                metrics_reader=metrics_reader,
                step=args.step,
                confirm_frames=args.confirm_frames,
                stable_frames=args.stable_frames,
            )
        except Exception as e:
            print(f"\n    Error scanning round {event.label}: {e}", file=sys.stderr)
            continue

        for frame_idx, time_ms, actions in results:
            for action in actions:
                action_data = {
                    "round": event.label,
                    "frame": frame_idx,
                    "time_ms": time_ms,
                    "kind": action.kind,
                    "cost": action.cost,
                    "gold_before": action.gold_before,
                    "gold_after": action.gold_after,
                    "unit_cost": action.unit_cost,
                    "unit_is_charm": action.unit_is_charm,
                    "slot_idx": action.slot_idx,
                    "confidence": round(action.confidence, 2),
                }
                all_spends.append(action_data)

    if not args.quiet:
        print(file=sys.stderr)

    if args.json:
        import json
        print(json.dumps(all_spends, indent=2))
    else:
        if not all_spends:
            print("(no spends detected)")
        else:
            headers = ["round", "time", "kind", "cost", "gold", "unit_cost", "charm", "slot", "conf"]
            rows = []
            for s in all_spends:
                rows.append((
                    s["round"],
                    format_time(s["time_ms"]),
                    s["kind"],
                    str(s["cost"]),
                    f"{s['gold_before']}->{s['gold_after']}",
                    str(s["unit_cost"] or "-"),
                    "Y" if s["unit_is_charm"] else "N",
                    str(s["slot_idx"] or "-"),
                    f"{s['confidence']:.2f}",
                ))
            _print_table(headers, rows)

    if not args.write or args.dry_run:
        if not args.json and not args.quiet:
            print("dry run: nothing written (pass --write to store these spends)", file=sys.stderr)
        return 0

    # Write to database
    written = 0
    for s in all_spends:
        if s["confidence"] < 0.5:
            continue  # skip low confidence
        # Find the event_id for this round
        round_event = store.find_event(game.id, "round", s["round"])
        if round_event:
            unit_id = None
            if s["unit_cost"] and s["kind"] in ("unit", "charm"):
                # Try to find matching unit in catalog
                units = store.list_units(cost=s["unit_cost"])
                if units:
                    # For now just use the first match
                    unit_id = units[0]["id"]
            store.add_spend(
                game_id=game.id,
                event_id=round_event.id,
                kind=s["kind"],
                cost=s["cost"],
                gold_before=s["gold_before"],
                gold_after=s["gold_after"],
                frame=s["frame"],
                time_ms=s["time_ms"],
                unit_id=unit_id,
                meta={"unit_is_charm": s["unit_is_charm"], "slot_idx": s["slot_idx"], "confidence": s["confidence"]}
            )
            written += 1

    print(f"game {game.id} ({game.title}): {written} spends written")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tftreview", description="TFT VOD review tool")
    p.add_argument("--db", help="library database path (default: app data folder)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("ui", help="open the player"); s.add_argument("video", nargs="?"); s.set_defaults(func=cmd_ui)
    s = sub.add_parser("db", help="print the library path"); s.set_defaults(func=cmd_db)
    s = sub.add_parser("new", help="register a video as a game"); s.add_argument("video"); s.add_argument("--title"); s.set_defaults(func=cmd_new)
    s = sub.add_parser("games", help="list games"); s.set_defaults(func=cmd_games)
    s = sub.add_parser("events", help="list a game's events"); s.add_argument("game"); s.set_defaults(func=cmd_events)
    s = sub.add_parser("stamp", help="mark a round at a time"); s.add_argument("game"); s.add_argument("label"); s.add_argument("time"); s.set_defaults(func=cmd_stamp)
    s = sub.add_parser("stamp-next", help="mark the next expected round"); s.add_argument("game"); s.add_argument("time"); s.set_defaults(func=cmd_stamp_next)
    s = sub.add_parser("marker", help="add a bookmark"); s.add_argument("game"); s.add_argument("time"); s.add_argument("label", nargs="?", default="mark"); s.set_defaults(func=cmd_marker)
    s = sub.add_parser("add", help="add a note/tag/metric line (e.g. 'gold=32 #bad-roll text')")
    s.add_argument("game"); s.add_argument("text", nargs="+")
    s.add_argument("--event", help="round label to attach to, e.g. 3-2")
    s.add_argument("--at", help="playhead time; attaches to the round in progress then")
    s.add_argument("--game-level", action="store_true", help="attach to the whole game")
    s.set_defaults(func=cmd_add)
    s = sub.add_parser("notes", help="list every entry of a game"); s.add_argument("game"); s.set_defaults(func=cmd_notes)

    s = sub.add_parser("analyze", help="metric / compare / corr / table / tags")
    s.add_argument("what", choices=["metric", "compare", "corr", "table", "tags"])
    s.add_argument("params", nargs="*")
    s.add_argument("--game", action="append", help="limit to a game (repeatable)")
    s.add_argument("--delta", action="append", help="add <key>_delta for a metric (repeatable)")
    s.add_argument("--scope", choices=["event", "game", "any"], default="event", help="where tags are looked up (compare)")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("detect", help="find round start timestamps in a video")
    s.add_argument("video")
    s.add_argument("--from", dest="start", metavar="TIME",
                   help="start time (0:00, 90, 90000ms)")
    s.add_argument("--to", dest="end", metavar="TIME", help="stop time")
    s.add_argument("--stage", type=int, metavar="N",
                   help="assume the scan starts inside stage N (use with --from)")
    s.add_argument("--write", action="store_true",
                   help="store the rounds in the library (default is a dry run)")
    s.add_argument("--dry-run", action="store_true", help="never write, even with --write")
    s.add_argument("--force", action="store_true",
                   help="overwrite rounds that are already marked")
    s.add_argument("--sidecar", action="store_true",
                   help="also write <video>.tftreview.json after --write")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--debug-dir", metavar="DIR",
                   help="save the HUD strip at each detected round")
    s.add_argument("--confirm", type=int, default=3, metavar="FRAMES",
                   help="frames a HUD reading must hold before it counts (default 3)")
    s.add_argument("--frame-stride", type=int, default=1, metavar="N",
                   help="process every N frames (default 1). e.g. 2 at 60fps = 30fps effective")
    s.add_argument("--threads", type=int, default=0, metavar="N",
                   help="ffmpeg decode threads (0=auto with fallback to 1, 1=single-threaded)")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_detect)

    s = sub.add_parser("read-metrics", help="read gold/level/XP/HP from the HUD at round starts")
    s.add_argument("game")
    s.add_argument("--round", action="append", metavar="LABEL",
                   help="only read this round (repeatable), e.g. 3-2")
    s.add_argument("--step", type=int, default=10, metavar="FRAMES",
                   help="frame stride while scanning a round (default 10)")
    s.add_argument("--wait", type=float, default=45.0, metavar="SECONDS",
                   help="how long to wait for the HUD to become readable (default 45)")
    s.add_argument("--late-wait", type=float, default=60.0, metavar="SECONDS",
                   help="how far into a round to look for the reference values (default 60)")
    s.add_argument("--write", action="store_true",
                   help="store the readings as metrics on each round (default is a dry run)")
    s.add_argument("--dry-run", action="store_true", help="never write, even with --write")
    s.add_argument("--force", action="store_true",
                   help="replace metric values already entered by hand (snapshotted first)")
    s.add_argument("--snapshot", metavar="PATH",
                   help="where to save replaced reference values "
                        "(default: <video>.tftreview.refs.json)")
    s.add_argument("--sidecar", action="store_true",
                   help="also write <video>.tftreview.json after --write")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_read_metrics)

    s = sub.add_parser("export", help="write csv / entries / sidecar")
    s.add_argument("what", choices=["csv", "entries", "sidecar"]); s.add_argument("path", nargs="?")
    s.add_argument("--game", action="append"); s.add_argument("--delta", action="append")
    s.set_defaults(func=cmd_export)
    s = sub.add_parser("import", help="import a sidecar json"); s.add_argument("path"); s.add_argument("--video"); s.add_argument("--replace", action="store_true"); s.set_defaults(func=cmd_import)
    s = sub.add_parser("rounds", help="show the active round sequence config"); s.set_defaults(func=cmd_rounds)
    s = sub.add_parser("hotkeys", help="show the hotkey cheat sheet"); s.set_defaults(func=cmd_hotkeys)

    s = sub.add_parser("build-catalog", help="scan shop slots during planning phases to build a unit catalog")
    s.add_argument("--games", help="comma-separated game IDs to scan")
    s.add_argument("--all", action="store_true", help="scan all games in database")
    s.add_argument("--output", default="units.csv", help="output CSV path")
    s.add_argument("--set", default="Unknown", help="set name for catalog (e.g. Set12)")
    s.add_argument("--step", type=int, default=30, help="frame sampling step (default 30 = 0.5s at 60fps)")
    s.add_argument("--max-rounds", type=int, default=10, help="max rounds per game to scan (for testing)")
    s.add_argument("--debug-dir", help="save debug frames to directory")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_build_catalog)

    s = sub.add_parser("scan-spends", help="scan for gold spend actions (roll/xp/unit/charm) during rounds")
    s.add_argument("game")
    s.add_argument("--round", action="append", metavar="LABEL",
                   help="only scan this round (repeatable), e.g. 3-2")
    s.add_argument("--step", type=int, default=1, metavar="FRAMES",
                   help="frame stride while scanning (default 1)")
    s.add_argument("--confirm-frames", type=int, default=1, metavar="N",
                   help="frames a shop change must persist to confirm (default 1)")
    s.add_argument("--stable-frames", type=int, default=1, metavar="N",
                   help="frames for temporal smoothing of slot state (default 1)")
    s.add_argument("--write", action="store_true",
                   help="store the spends in the database (default is a dry run)")
    s.add_argument("--dry-run", action="store_true", help="never write, even with --write")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_scan_spends)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.db or paths.db_path())
    try:
        if args.command == "export" and args.what != "sidecar" and not args.path:
            print("export needs an output path", file=sys.stderr)
            return 2
        return args.func(args, store)
    except (LookupError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            store.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
