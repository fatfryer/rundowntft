# TFT VOD Review

A hotkey-driven VOD reviewer for Teamfight Tactics. Mark where each round starts, jump between rounds with a keypress, and attach notes, tags and numbers (gold, HP, anything) to rounds and games. Everything you record is stored in one uniform way, so it can all be analyzed later.

> Status: early, but the core loop works. The player, round marking, notes/tags/metrics, storage, analysis, export, **automatic round detection** and **gold / level / XP / HP reading** are built. Next up: showing the readings in the UI and detecting within-round changes.

*Not affiliated with or endorsed by Riot Games. Teamfight Tactics is a trademark of Riot Games, Inc.*

## Install

Requires Python 3.10+.

```bash
git clone <your-repo-url> tft-vod-review
cd tft-vod-review
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -e ".[ui,dev]"
```

## Run

```bash
tftreview ui                    # open the player (Ctrl+O to pick a VOD)
tftreview ui path/to/game.mp4   # or open one directly
```

If audio misbehaves on your machine, run with `TFTREVIEW_NO_AUDIO=1` to play silently.

## How reviewing works

1. Open a VOD. Press **Space** to play.
2. When a round starts, press **M**. The app knows the order of rounds (1-1, 1-2, ..., 2-1, ...) so it labels each mark automatically. Missed one? **Shift+M** lets you type a label, and marking a label that already exists just moves it.
3. Now **N** / **P** jump to the next / previous round. **1** to **7** jump to the start of that stage.
4. Press **Enter**, type, press **Enter** again to save. What you type is parsed:

   | You type | You get |
   |---|---|
   | `gold=32` | a **metric** named `gold` with value 32 |
   | `#bad-roll` | a **tag** named `bad-roll` |
   | anything else | a **note** |

   Mix them freely: `gold=32 hp=71 #bad-roll should have slammed the item`.
   Notes attach to the round at the playhead. **Shift+Enter** saves to the whole game instead (good for `place=3 #fast9`).
5. Your work autosaves to a `.tftreview.json` file next to the VOD and into a local library database.

### Hotkeys

| Key | Action |
|---|---|
| Space / C / K | Play / pause |
| V / X | Forward / back one frame |
| Shift+V / Shift+X | Forward / back 1 second |
| Left / Right | Back / forward 5 s |
| Ctrl+Left / Right | Back / forward 30 s |
| F or N or ] / D or P or [ | Next / previous event (D restarts the current round unless you just started it) |
| M | Mark the next expected round here |
| Shift+M | Mark a specific round label here |
| Ctrl+Backspace | Remove the most recently marked round |
| B | Add a free-form bookmark |
| Enter / Shift+Enter | Write a note on the round / on the game (press again to save) |
| Esc | Cancel what you're typing |
| 1-7 | Jump to the start of stage N |
| = / - / 0 | Faster / slower / normal speed |
| Ctrl+O, F1 | Open video, show this list |

The status bar shows the current frame (`f 700 / 99,302 (60 fps)`), which is handy when checking whether a detected timestamp is right on the round switch.

Everything is rebindable: put a `keymap.json` in the app data folder (see `tftreview db` for the location), e.g. `{"next_event": ["Down"], "prev_event": ["Up"]}`. `tftreview hotkeys` prints the active bindings and warns about conflicts.

## Detecting round starts automatically

Detection reads the row of round icons along the top of the TFT HUD: the round in progress is gold, the others are grey, and the row re-centres when the stage changes (4 icons in stage 1, 7 from stage 2 on). So the "which round is it" question is just gold density in each icon box - no OCR. A whole 27-minute VOD takes about 2.5 minutes to scan. `ffmpeg` must be on your PATH.

```bash
tftreview detect game.mp4                     # dry run: print what it found
tftreview detect game.mp4 --json              # same, machine-readable
tftreview detect game.mp4 --write             # store the rounds in the library
tftreview detect game.mp4 --write --sidecar   # ...and write <video>.tftreview.json
```

Then open the video as usual and **F** / **D** jump between the detected rounds. Rounds that are already in the library (for example ones you marked by hand) are left alone unless you add `--force`. Useful extras:

```bash
tftreview detect game.mp4 --to 10:00              # scan only the first 10 minutes
tftreview detect game.mp4 --from 30:00 --stage 3  # resume mid-game (tell it the stage)
tftreview detect game.mp4 --confirm 5             # demand 5 matching frames (stricter)
tftreview detect game.mp4 --debug-dir /tmp/hud    # save the icon strip at each round
```

The round counts come from `rounds.json` (see [Data](#data)), so a new set layout needs no code change.

## Reading gold, level, XP and HP

`read-metrics` scans each detected round for the HUD numbers and stores them as ordinary metrics (`gold`, `level`, `xp`, `xp_next`, `hp`) on the round. It reads the rendered digits directly - the same HUD font at any resolution - so no OCR package is needed.

```bash
tftreview read-metrics game.mp4                  # dry run: a table per round
tftreview read-metrics game.mp4 --write          # store the readings on each round
tftreview read-metrics game.mp4 --round 3-2      # just one round (repeatable)
```

A round start is sometimes covered by an augment overlay or a fade, so the scanner walks the round in 10-frame steps and accepts the first reading that the next sample confirms. Carousel rounds (`*-4`) have no readable HUD and are skipped.

Every reading is checked against metrics you already typed on that round. The verdict column tells you what happened: `ok` (the start matches your note), `late` (your note shows up later in the round - common on augment rounds, where gold is spent after the round opens), `bad` (never matched), `read` (no reference recorded), or `carousel` (skipped).

`--write` never overwrites values you entered by hand. To replace them with the machine readings, add `--force`; the old values are saved to `<video>.tftreview.refs.json` first (override with `--snapshot PATH`). Use `--json` for machine-readable output.

The digit templates ship in `src/tftreview/vision/glyph_templates.npz`. To rebuild them after a UI-scale change, label a reviewed game with `python scripts/build_glyph_templates.py --video game.mp4` (it learns from your notes, level and HP only).

## Analyzing what you recorded

Notes, tags and metrics all live in one table, and **gold is not special**: it is just a metric you typed. So every analysis works for anything you record.

```bash
tftreview games                                   # your library
tftreview analyze metric gold                     # gold at each round across all games
tftreview analyze compare gold rolldown           # gold on rounds tagged #rolldown vs not
tftreview analyze corr gold game:place            # does gold at a round relate to final placement?
tftreview analyze table --delta gold              # every round, plus the change in gold since the last sample
tftreview analyze tags                            # how often you use each tag
tftreview export csv rounds.csv --delta gold      # the same table for Excel / pandas
```

Game-level metrics are available on every round with a `game:` prefix, which is what makes "gold at 4-1 vs final placement" a one-liner. In Python:

```python
from tftreview import analysis
from tftreview.store import Store

store = Store("path/to/library.sqlite3")
rows = analysis.with_deltas(analysis.event_table(store), "gold")
analysis.correlate(rows, "gold_delta", "game:place")
```

Everything is also scriptable without the UI: `tftreview new`, `stamp`, `add`, `notes`, `events` (see `tftreview --help`).

## Data

| What | Where |
|---|---|
| Library (all games, queryable with SQL) | `library.sqlite3` in the app data folder (`tftreview db` prints the path) |
| Portable per-game copy | `<video>.tftreview.json` next to each VOD (human-readable; share it with a coach, `tftreview import` it elsewhere) |

If a VOD isn't in your library yet but has a sidecar next to it, opening it imports the sidecar.

The expected round order lives in data, not code. `tftreview rounds` prints the active sequence; save an edited copy as `rounds.json` in the app data folder to change it (for a new set, say). **The built-in default (carousel at 1-1 and x-4, PvE at 1-2 to 1-4 and x-7) is a best guess: check it against your own VODs.**

## Project layout

```
src/tftreview/
  models.py        # Game / Event / Entry (note | tag | metric)
  store.py         # SQLite
  rounds.py        # round sequence config
  nav.py           # next / previous event logic
  entry_parser.py  # "gold=32 #tag text" -> metrics, tags, note
  session.py       # ReviewSession: all review logic, no Qt
  analysis.py      # summaries, comparisons, correlations
  export.py        # CSV and the JSON sidecar
  keymap.py        # default hotkeys
  cli.py           # command line
  ui/              # Qt: player.py, timeline.py, main_window.py, app.py
  vision/          # video.py, hud.py, detect.py (rounds), glyphs.py + metrics.py (numbers)
tests/
```

The UI is deliberately thin: all review logic lives in `session.py` and is tested without a display. The Qt player is wrapped in one small class (`ui/player.py`), so swapping in libmpv later is a one-file change. The detector and the metric reader are Qt-free too and are driven from the same core, which is why `tftreview detect` / `read-metrics` and the UI see identical timestamps.

## Tests

```bash
pytest
```

The UI smoke test runs headless (offscreen, no audio) and skips itself if PySide6 isn't installed.

## Roadmap

- [x] Player, round marking, hotkey navigation, notes / tags / metrics, analysis, export
- [x] Round detection from video (HUD icon row -> round-start timestamps)
- [x] Gold / HP / level / XP reading per round, auto-recorded as metrics
- [ ] Show auto-read metrics in the player; detect within-round changes
- [ ] Shop / unit recognition
- [ ] Cross-game library view and charts
- [ ] Installer and releases

## License

Not chosen yet. Add a `LICENSE` file before publishing. Without one, nobody else is legally allowed to use or modify the code. MIT is a common permissive default.
