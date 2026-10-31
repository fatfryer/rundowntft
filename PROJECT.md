# TFT VOD Review - Project Roadmap

## Project Overview
A hotkey-driven VOD reviewer for Teamfight Tactics that combines video playback, event tracking, note-taking, and analytics in one tool. Built for reviewers who need to quickly cycle through rounds, annotate games, and analyze metrics across sessions.

**Current Version:** 0.1.0  
**Status:** Core functionality complete - manual round marking, notes/tags/metrics, SQLite storage, CLI analysis, Qt UI  
**Next Major Milestone:** Automatic round detection from video (computer vision pipeline)

---

## Architecture Summary

```
src/tftreview/
├── models.py          # Game, Event, Entry dataclasses
├── store.py           # SQLite persistence (library + sidecars)
├── rounds.py          # Round sequence config (data-driven)
├── nav.py             # Event navigation (bisect-based)
├── entry_parser.py    # "gold=32 #tag note" parser
├── session.py         # ReviewSession - headless review logic
├── analysis.py        # Metric stats, comparisons, correlations
├── export.py          # CSV + JSON sidecar import/export
├── cli.py             # Full CLI (ui, games, analyze, export...)
├── keymap.py          # Hotkey definitions + conflict detection
├── timeutil.py        # ms ↔ HH:MM:SS parsing/formatting
├── paths.py           # App data directories
└── ui/
    ├── player.py      # QMediaPlayer wrapper (swappable)
    ├── timeline.py    # Visual seek bar with event markers
    ├── main_window.py # Main review window (video + panels)
    └── app.py         # Qt application entry point
```

**Key Design Decisions:**
- Headless core (`session.py`) - all review logic testable without Qt
- Single entry table for notes/tags/metrics - unified analysis
- Game-level metrics inherited onto event rows with `game:` prefix
- Round sequence in data (`rounds.json`) not code - set changes = config edit
- Qt player isolated in `player.py` - libmpv swap is one file

---

## ✅ What's DONE (v0.1.0)

### Video Playback & Navigation
- [x] Qt/QMediaPlayer wrapper with audio toggle (`TFTREVIEW_NO_AUDIO=1`)
- [x] Seek controls: ±1s, ±5s, ±30s, variable playback speed (0.25x-4x)
- [x] Timeline bar with colored event markers (PvP/PvE/Carousel/Bookmark)

### Event Tracking (Manual)
- [x] Round marking with automatic sequence (M = next expected, Shift+M = specific label)
- [x] Round sequence config: stages 1-7, carousel at 1-1/*-4, PvE at 1-2..1-4/*-7
- [x] Event navigation: N/P (next/prev round), 1-7 (jump to stage start)
- [x] Free-form bookmarks (B key)
- [x] Move/rename/delete events via context menu

### Notes, Tags & Metrics
- [x] Quick-entry syntax: `gold=32 hp=71 #bad-roll #comp:fast9 note text`
- [x] Three entry types: metrics (numbers), tags (#labels), notes (free text)
- [x] Scope: event-level (round) or game-level (Shift+Enter)
- [x] Metrics overwrite (one value per key per target), tags deduplicate
- [x] Entry editing/deletion via context menu

### Storage & Portability
- [x] SQLite library (`library.sqlite3`) with games, events, entries tables
- [x] Portable JSON sidecars (`.tftreview.json` next to video)
- [x] Auto-import sidecar when opening unindexed video
- [x] Autosave sidecar on changes (1.5s debounce) + on close

### Analysis & Export (CLI)
- [x] `tftreview analyze metric <key>` - stats per round across games
- [x] `tftreview analyze compare <metric> <tag>` - metric with/without tag
- [x] `tftreview analyze corr <metric_a> <metric_b>` - Pearson correlation
- [x] `tftreview analyze table` - flat round table with all metrics
- [x] `tftreview analyze tags` - tag frequency counts
- [x] `tftreview analyze ... --delta <key>` - round-over-round changes
- [x] `tftreview export csv` / `entries` / `sidecar`
- [x] `tftreview import` sidecar to another machine

### Configuration & Customization
- [x] `rounds.json` override for custom set layouts
- [x] `keymap.json` override for custom hotkeys (with conflict warnings)
- [x] `tftreview rounds` / `hotkeys` commands show active config

### Testing
- [x] Store tests (CRUD, cascades, scopes, persistence)
- [x] Parser/time tests (entry syntax, time formats)
- [x] CLI integration tests (full review workflow)
- [x] Rounds/nav/session tests (sequence, navigation, edge cases)
- [x] Analysis/export tests (tables, stats, correlations, sidecar roundtrip)
- [x] UI smoke tests (headless, drives hotkey actions, verifies panels)

---

## 🎯 IMMEDIATE NEXT STEPS (v0.2 - Automatic Round Detection)

### Phase 1: Video Frame Extraction & Template Matching
- [ ] Frame extraction pipeline (sample at intervals, e.g., every 1-2 seconds)
- [ ] Stage number template matching (OCR or template matching for "1-1", "2-3", etc.)
- [ ] Binary search for round start boundaries between detected stage numbers
- [ ] Confidence scoring & manual verification UI

### Phase 2: Auto-Stamp Integration
- [ ] "Auto-detect rounds" button in UI (runs detection, pre-populates events)
- [ ] Source tracking: `source="auto"` vs `source="manual"` on events
- [ ] One-click accept/adjust/reject for each auto-detected round
- [ ] Fallback to manual when detection confidence is low

### Phase 3: Gold/HP Reading (v0.3)
- [ ] ROI definition for gold/HP UI regions (configurable per resolution)
- [ ] Digit recognition (template matching or lightweight CNN)
- [ ] Per-round auto-metric recording: `gold=32 hp=71` on round start
- [ ] Verification overlay showing detected values vs manual entry

---

## 📊 MEDIUM TERM (v0.4 - v0.6)

### v0.4: Shop & Unit Recognition
- [ ] Shop state detection (units available, costs, reroll cost)
- [ ] Board state recognition (placed units, items, traits)
- [ ] Augment tracking (when offered, when taken)
- [ ] Structured data model for shop/board snapshots per round

### v0.5: Cross-Game Library View & Charts (UI)
- [ ] Library browser in UI (game list with filters: date, placement, tags)
- [ ] Per-metric charts across games (gold at 3-2 over last 20 games)
- [ ] Tag-based comparison views (winrate with #fast9 vs without)
- [ ] Placement distribution charts
- [ ] Export charts as images

### v0.6: Advanced Analytics
- [ ] Rolling averages / trend lines for metrics
- [ ] Round-type specific analysis (carousel vs PvP vs PvE spending)
- [ ] "What if" simulation: gold spent at X-4 vs outcome
- [ ] Coach-sharing: annotated VOD + sidecar bundle export

---

## 🚀 LONG TERM (v1.0+)

### v1.0: Polish & Distribution
- [ ] Installer (Windows .exe, macOS .dmg, Linux AppImage/Flatpak)
- [ ] Auto-update mechanism
- [ ] First-run wizard (round config verification, hotkey tour)
- [ ] Performance optimization (lazy loading, background indexing)
- [ ] Accessibility improvements

### v1.1+: Community Features
- [ ] Plugin system for custom metrics/parsers
- [ ] Shared round configs per set (community maintained)
- [ ] Cloud sync for library (optional, encrypted)
- [ ] Multi-language support

---

## 🔧 Technical Debt & Quality

### Code Quality
- [ ] Add type hints to all public APIs (mostly done)
- [ ] Increase test coverage for UI components
- [ ] Add integration test for full auto-detect → analyze pipeline
- [ ] Benchmark SQLite queries on 1000+ game libraries

### Architecture
- [ ] Extract video analysis into separate process (avoid blocking UI)
- [ ] Consider libmpv for frame-accurate seeking (current Qt seeking has ~200ms latency)
- [ ] Event sourcing for review sessions (undo/redo, replay)

---

## 📋 Feature Requests (from Issues/Discussions)

| Feature | Priority | Effort | Notes |
|---------|----------|--------|-------|
| Multiple video support (split VODs) | Medium | Medium | One game = multiple files |
| Keyboard macro recording | Low | Low | Replay common annotation sequences |
| Voice notes (audio annotations) | Low | High | Attach audio clips to rounds |
| Twitch/YouTube URL direct open | Medium | Medium | yt-dlp integration |
| Overlay mode (always-on-top minimal UI) | Medium | Low | For second monitor review |

---

## 🎮 Game-Specific Notes (TFT)

**Current Set Assumptions (verify per set):**
- Carousel at 1-1 and every *-4
- PvE at 1-2, 1-3, 1-4 and every *-7
- 7 rounds per stage after stage 1
- 7 stages max

**Config Location:** `tftreview rounds` shows path to `rounds.json` override

**Sidecar Format:** Human-readable JSON, versioned, forward-compatible

---

## 📈 Success Metrics

| Metric | Current | Target (v0.5) |
|--------|---------|---------------|
| Time to annotate one game | ~15 min | ~5 min (with auto-detect) |
| Round detection accuracy | N/A (manual) | >95% |
| Games in library before slowdown | ~500 | 5000+ |
| CLI analysis latency | <1s | <500ms |
| UI startup time | ~2s | <1s |

---

## 👥 Team & Ownership

| Area | Owner | Status |
|------|-------|--------|
| Core logic (store, session, analysis) | - | Stable |
| Qt UI (player, timeline, main_window) | - | Functional |
| CLI & configuration | - | Complete |
| Video analysis (CV pipeline) | - | **Next focus** |
| Distribution/packaging | - | Not started |

---

*Last updated: 2026-10-24*
*Update this file when completing milestones or reprioritizing*