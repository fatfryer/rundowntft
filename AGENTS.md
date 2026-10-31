# AGENTS.md - Guidelines for AI Agents Working on TFT VOD Review

## Project Context
This is a **TFT VOD Review tool** - a hotkey-driven video player + annotation + analytics suite for Teamfight Tactics VOD review. The core value prop: reviewers mark round starts with a hotkey, jump between rounds instantly, annotate with structured data (metrics/tags/notes), and analyze across games via CLI or (future) UI charts.

**Current State:** v0.1.0 - Manual round marking, full annotation system, SQLite storage, CLI analysis, Qt UI. **Next: Automatic round detection via computer vision.**

---

## How to Work on This Project

### 1. Read These Files First (Architecture Understanding)
```
src/tftreview/models.py        # Data model: Game, Event, Entry (note/tag/metric)
src/tftreview/session.py       # ReviewSession - ALL review logic, no Qt
src/tftreview/store.py         # SQLite schema + CRUD
src/tftreview/rounds.py        # Round sequence config (data-driven)
src/tftreview/nav.py           # Event navigation (bisect, pure functions)
src/tftreview/analysis.py      # Stats, comparisons, correlations
```

### 2. Key Architectural Principles
- **Headless core first:** All logic in `session.py`/`analysis.py`/etc. - testable without display
- **UI is thin:** `ui/main_window.py` wires hotkeys → `ReviewSession` methods
- **Single entry table:** Notes, tags, metrics share one schema → unified analysis
- **Game metrics inherit:** `game:place=3` appears on every round row for correlation
- **Config over code:** Round sequence in `rounds.json`, hotkeys in `keymap.json`
- **Player is swappable:** `ui/player.py` is the ONLY Qt multimedia touchpoint

### 3. Development Workflow
```bash
# Install deps
pip install -e ".[ui,dev]"

# Run tests (headless, fast)
pytest

# Run UI (needs display or QT_QPA_PLATFORM=offscreen)
tftreview ui                    # open player
tftreview ui path/to/game.mp4   # open specific video

# CLI workflow
tftreview new game.mp4
tftreview stamp-next game.mp4 10:00
tftreview add game.mp4 "gold=32 #rolldown"
tftreview analyze metric gold
tftreview export csv rounds.csv --delta gold
```

### 4. Testing Strategy
- **Unit tests** in `tests/` - run with `pytest` (no display needed)
- **UI smoke test** (`test_ui_smoke.py`) - runs headless with faked player position
- **Integration test** (`test_cli.py::test_end_to_end_review_workflow`) - full workflow
- **Add tests for new logic in `session.py`/`analysis.py`/`nav.py`** - these are pure and easy to test
- **UI tests only for regression** - prefer testing the headless core

### 5. Common Tasks & Where to Make Changes

| Task | Files to Touch |
|------|----------------|
| Add new analysis function | `analysis.py` + `cli.py` (analyze command) |
| Add new entry type | `models.py` (ENTRY_KINDS), `store.py`, `entry_parser.py`, `session.py` |
| Change round sequence | `rounds.py` (default_config) or user drops `rounds.json` |
| Add hotkey | `keymap.py` (DEFAULT_KEYMAP, ACTION_DOCS) + `ui/main_window.py` (_build_actions) |
| Modify timeline viz | `ui/timeline.py` (paintEvent) |
| Change video backend | `ui/player.py` ONLY (QMediaPlayer → libmpv) |
| Add CLI command | `cli.py` (add subparser + handler function) |
| Modify sidecar format | `export.py` (SIDECAR_VERSION, game_to_dict, import_game_dict) |

### 6. Code Style & Conventions
- **Type hints** on all public functions (use `from __future__ import annotations`)
- **Dataclasses** for data objects (`models.py`, `rounds.py`, `analysis.py`)
- **Pure functions** where possible (`nav.py`, `entry_parser.py`, `analysis.py` helpers)
- **No Qt in core** - only in `ui/` package
- **Errors as exceptions** - CLI catches and prints, UI shows in status bar
- **Configurable via files** - not env vars (except `TFTREVIEW_NO_AUDIO`, `TFTREVIEW_HOME`)

### 7. Database Schema (store.py)
```sql
games:       id, video_path(UNIQUE), title, created_at, meta(JSON)
events:      id, game_id, kind(round|marker), label, time_ms, source, meta(JSON)
entries:     id, game_id, event_id(NULL=game-level), kind(note|tag|metric), key, num, text, time_ms, created_at
Indexes:     events(game_id, time_ms), entries(game_id), entries(event_id), entries(kind, key)
```
- **Foreign keys ON** with CASCADE (deleting event → deletes its entries)
- **One metric per (target, key)** - `set_metric` overwrites
- **One tag per (target, key)** - `add_tag` is idempotent

### 8. Quick-Entry Syntax (entry_parser.py)
```
gold=32          → metric "gold" = 32.0
hp=71.5          → metric "hp" = 71.5
#bad-roll        → tag "bad-roll"
#comp:fast9      → tag "comp:fast9" (namespacing via naming convention)
should slam      → note "should slam"
gold=32 #tag note → all three at once
```
- Case-insensitive, normalized to lowercase
- Non-numeric `key=value` stays as note text

### 9. Round Sequence Config (rounds.py)
```python
RoundConfig(
    name="ranked-default",
    stages={1: 4, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 7},
    default_type="pvp",
    overrides={
        "1-1": "carousel",
        "1-2": "pve", "1-3": "pve", "1-4": "pve",
        "*-4": "carousel",   # wildcard for every stage-4
        "*-7": "pve",        # wildcard for every stage-7
    }
)
```
- Users override by saving JSON to `rounds.json` (path via `tftreview rounds`)
- Labels outside config accepted if they match `S-R` pattern

### 10. Video Analysis Pipeline (THE NEXT BIG THING)
When implementing auto-detection, follow this pattern:

```
src/tftreview/vision/
├── frames.py           # Frame extraction (ffmpeg/opencv, sampling strategy)
├── templates.py        # Template assets + matching (stage numbers, gold digits)
├── detect.py           # Pipeline: extract → match → binary search → events
├── roi.py              # Region-of-interest definitions (resolution-independent)
└── verify.py           # Confidence scoring + UI for accept/adjust/reject
```

**Integration points:**
- `session.py` gets `stamp_auto(events: list[Event])` method
- Events get `source="auto"` + `meta={"confidence": 0.95, "method": "template"}`
- UI: "Auto-detect rounds" button → runs in background → shows preview dialog

### 11. Performance Considerations
- **SQLite:** Use indexes, avoid N+1 queries (analysis.py uses batch loads)
- **UI:** Timeline repaints on every position change - debounce if needed
- **Video:** Qt seeking has latency; libmpv would be frame-accurate
- **Large libraries:** 1000+ games still fast (<1s analysis) but monitor

### 12. Release Checklist (for v0.2+)
- [ ] All tests pass (`pytest -x`)
- [ ] CLI help text updated for new commands
- [ ] README updated if user-facing changes
- [ ] `pyproject.toml` version bumped
- [ ] Sidecar format version bumped if schema changed
- [ ] Migration path for existing users (sidecar re-import works)

---

## 🚫 Things NOT To Do
- Don't put Qt imports in non-`ui/` modules
- Don't add columns to tables without migration plan (sidecar version handles this)
- Don't make gold/HP special - they're just metrics
- Don't hardcode round sequences - use `rounds.json`
- Don't block UI thread with video analysis (use QThread/process pool)

---

## 📞 When Stuck
- Check `PROJECT.md` for roadmap context
- Run `tftreview --help` and `tftreview <cmd> --help` for CLI reference
- Look at `tests/test_cli.py::test_end_to_end_review_workflow` for usage patterns
- UI behavior: run `test_ui_smoke.py` tests to see expected interactions

---

*This file is for AI agents. Humans: see PROJECT.md for roadmap, README.md for usage.*