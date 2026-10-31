"""Shop slot detection and unit parsing.

The TFT shop shows 5 unit cards in a horizontal row during the planning phase.
Each card has:
- Unit name (white text, top-left)
- Cost (1-5, gray text on gold badge, top-right)
- Traits (icons + text, below name)
- Charm indicator (pink background)

Geometry at 1920x1080:
- Shop row: y=750-930 (180px tall), x=560-1360 (800px wide, 5 slots)
- Each slot: 160px wide
- Cost badge: top-right 30x40 of each slot
- Unit name region: top-left ~100x50 of each slot
- Traits region: below name, ~160x100
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional

import cv2
import numpy as np

from . import glyphs
from .glyphs import FONT16, FontSpec, Glyph
from .video import VideoMeta

# ---------------------------------------------------------------- 1080p geometry
REF_W, REF_H = 1920, 1080

# Shop row (5 slots horizontal)
SHOP_Y0, SHOP_Y1 = 750, 930          # 180px tall
SHOP_X0, SHOP_X1 = 560, 1360         # 800px wide (5 * 160)
SLOT_WIDTH = 160
N_SLOTS = 5

# Per-slot regions (relative to slot origin)
COST_BADGE = (130, 0, 160, 40)       # x0, y0, x1, y1 relative to slot (30x40 at top-right)
NAME_BOX = (8, 4, 108, 48)           # unit name area (100x44 at top-left)
TRAITS_BOX = (8, 52, 152, 172)       # traits area (144x120 below name)

# Cost badge colors (HSV) - varies by TFT set/skin
# Set 12+ uses blue-purple badges: H~100-150
COST_BADGE_BLUE_LO = (95, 40, 25)    # blue/purple badge background
COST_BADGE_BLUE_HI = (155, 255, 230)
# Cost number: white/gray text on colored badge
COST_NUM_LO = (0, 0, 100)            # gray/white number on badge
COST_NUM_HI = (179, 100, 220)

# Unit name uses white text like HUD
NAME_V_MIN = 140
NAME_S_MAX = 80

# Charm detection (pink/magenta background) - true pink is H~160-175, high saturation
CHARM_LO = (160, 80, 80)
CHARM_HI = (175, 255, 255)


@dataclass(frozen=True)
class ShopModel:
    """Shop geometry scaled to video resolution."""

    width: int
    height: int
    sx: float
    sy: float

    @classmethod
    def for_meta(cls, meta: VideoMeta) -> "ShopModel":
        sx = meta.width / REF_W
        sy = meta.height / REF_H
        return cls(width=meta.width, height=meta.height, sx=sx, sy=sy)

    @property
    def shop_box(self) -> tuple[int, int, int, int]:
        """Full shop row as (x0, y0, x1, y1)."""
        return (
            round(SHOP_X0 * self.sx),
            round(SHOP_Y0 * self.sy),
            round(SHOP_X1 * self.sx),
            round(SHOP_Y1 * self.sy),
        )

    def slot_boxes(self) -> list[tuple[int, int, int, int]]:
        """Absolute pixel boxes for each of the 5 slots."""
        w = round(SLOT_WIDTH * self.sx)
        x0 = round(SHOP_X0 * self.sx)
        y0 = round(SHOP_Y0 * self.sy)
        y1 = round(SHOP_Y1 * self.sy)
        return [(x0 + i * w, y0, x0 + (i + 1) * w, y1) for i in range(N_SLOTS)]

    def cost_badge_box(self, slot_idx: int) -> tuple[int, int, int, int]:
        """Absolute cost badge box for a slot."""
        sx0, sy0, sx1, sy1 = self.slot_boxes()[slot_idx]
        bx0, by0, bx1, by1 = COST_BADGE
        return (
            sx0 + round(bx0 * self.sx),
            sy0 + round(by0 * self.sy),
            sx0 + round(bx1 * self.sx),
            sy0 + round(by1 * self.sy),
        )

    def name_box(self, slot_idx: int) -> tuple[int, int, int, int]:
        """Absolute unit name box for a slot."""
        sx0, sy0, _, _ = self.slot_boxes()[slot_idx]
        bx0, by0, bx1, by1 = NAME_BOX
        return (
            sx0 + round(bx0 * self.sx),
            sy0 + round(by0 * self.sy),
            sx0 + round(bx1 * self.sx),
            sy0 + round(by1 * self.sy),
        )

    def traits_box(self, slot_idx: int) -> tuple[int, int, int, int]:
        """Absolute traits box for a slot."""
        sx0, sy0, _, _ = self.slot_boxes()[slot_idx]
        bx0, by0, bx1, by1 = TRAITS_BOX
        return (
            sx0 + round(bx0 * self.sx),
            sy0 + round(by0 * self.sy),
            sx0 + round(bx1 * self.sx),
            sy0 + round(by1 * self.sy),
        )


@dataclass(frozen=True)
class UnitInSlot:
    """A unit detected in a shop slot."""

    name: Optional[str] = None
    cost: Optional[int] = None
    traits: tuple[str, ...] = ()
    is_charm: bool = False
    name_score: float = 0.0
    cost_score: float = 0.0

    def is_empty(self) -> bool:
        return self.name is None and self.cost is None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "cost": self.cost,
            "traits": list(self.traits),
            "is_charm": self.is_charm,
            "name_score": round(self.name_score, 3),
            "cost_score": round(self.cost_score, 3),
        }


class ShopReader:
    """Reads shop slots from a frame with temporal smoothing."""

    def __init__(
        self,
        model: ShopModel,
        templates: Optional[Mapping[str, Mapping[str, np.ndarray]]] = None,
        template_path: str | Path = glyphs.TEMPLATE_PATH,
        stable_frames: int = 10,
    ):
        self.model = model
        self.templates = dict(templates) if templates is not None else glyphs.load_templates(template_path)
        # Font for unit names (same as HUD gold/level)
        self.font16 = FONT16.scaled(model.sx, model.sy)
        # Cost numbers use same digit shapes as HUD (1-5), just smaller rendering
        # Use font16 templates but with adjusted detection thresholds
        self.cost_font = FontSpec(
            name="cost",
            size=FONT16.size,  # (12, 16) - match template size
            min_h=8,
            max_h=22,
            min_w=3,
            max_w=14,
            v_min=90,
            s_max=90,
            min_score=0.60,
        ).scaled(model.sx, model.sy)
        # Temporal smoothing
        self.stable_frames = stable_frames
        self._cost_history: list[list[Optional[int]]] = []  # per-slot cost history
        self._charm_history: list[list[bool]] = []  # per-slot charm history
        self._badge_history: list[list[bool]] = []  # per-slot badge presence history

    def read_slot(self, frame: np.ndarray, slot_idx: int) -> UnitInSlot:
        """Read one shop slot (raw detection, no smoothing)."""
        slot_box = self.model.slot_boxes()[slot_idx]
        x0, y0, x1, y1 = slot_box
        slot = frame[y0:y1, x0:x1]

        # Check for charm (pink background)
        is_charm = self._detect_charm(slot)

        # Read cost from badge
        cost, cost_score = self._read_cost(frame, slot_idx)

        # Detect badge presence (more reliable than cost reading)
        has_badge = self._detect_badge_presence(slot)

        # Read unit name
        name, name_score = self._read_name(frame, slot_idx)

        # Read traits (placeholder for now)
        traits = self._read_traits(frame, slot_idx)

        return UnitInSlot(
            name=name,
            cost=cost,
            traits=traits,
            is_charm=is_charm,
            name_score=name_score,
            cost_score=cost_score,
        )

    def read_shop(self, frame: np.ndarray) -> list[UnitInSlot]:
        """Read all 5 shop slots with temporal smoothing."""
        raw_slots = [self.read_slot(frame, i) for i in range(N_SLOTS)]

        # Initialize history on first call
        if not self._cost_history:
            self._cost_history = [[None] * self.stable_frames for _ in range(N_SLOTS)]
            self._charm_history = [[False] * self.stable_frames for _ in range(N_SLOTS)]
            self._badge_history = [[False] * self.stable_frames for _ in range(N_SLOTS)]

        # Update history
        for i, unit in enumerate(raw_slots):
            self._cost_history[i].append(unit.cost)
            self._charm_history[i].append(unit.is_charm)
            slot_box = self.model.slot_boxes()[i]
            slot_img = frame[slot_box[1]:slot_box[3], slot_box[0]:slot_box[2]]
            self._badge_history[i].append(self._detect_badge_presence(slot_img))
            if len(self._cost_history[i]) > self.stable_frames:
                self._cost_history[i].pop(0)
                self._charm_history[i].pop(0)
                self._badge_history[i].pop(0)

        # Compute stable state using mode (most frequent) over history
        stable_slots = []
        for i in range(N_SLOTS):
            # Cost: most frequent non-None cost in history
            cost_counts: dict[Optional[int], int] = {}
            for c in self._cost_history[i]:
                cost_counts[c] = cost_counts.get(c, 0) + 1

            best_cost = None
            best_count = 0
            for cost, count in cost_counts.items():
                if cost is not None and count > best_count:
                    best_cost = cost
                    best_count = count

            # Use cost if it appears in at least 30% of frames
            if best_count >= max(2, self.stable_frames // 3):
                stable_cost = best_cost
            else:
                stable_cost = None

            # Badge presence: true if appears in majority
            badge_count = sum(1 for b in self._badge_history[i] if b)
            stable_badge = badge_count >= self.stable_frames // 2

            # Charm: true if appears in majority
            charm_count = sum(1 for c in self._charm_history[i] if c)
            stable_charm = charm_count >= self.stable_frames // 2

            # Use the most recent raw slot with the stable cost/charm/badge
            raw = raw_slots[i]
            stable_slots.append(UnitInSlot(
                name=raw.name,
                cost=stable_cost,
                traits=raw.traits,
                is_charm=stable_charm,
                name_score=raw.name_score,
                cost_score=raw.cost_score,
            ))

        return stable_slots

    def _detect_charm(self, slot: np.ndarray) -> bool:
        """Check if slot has pink charm background.

        Charm cards have a distinctive pink/purple gradient covering most of the card.
        """
        hsv = cv2.cvtColor(slot, cv2.COLOR_BGR2HSV)
        pink = cv2.inRange(hsv, np.array(CHARM_LO, np.uint8), np.array(CHARM_HI, np.uint8))
        # Charm should have pink across most of the slot, not just a small region
        total_pink = pink.sum()
        slot_area = pink.shape[0] * pink.shape[1]
        pink_ratio = total_pink / slot_area
        # Also check upper portion has significant pink
        upper_pink = pink[:80, :].sum()
        # Exclude badge region (top-right)
        badge_pink = pink[:40, -30:].sum()
        non_badge_pink = upper_pink - badge_pink
        # True charm: >15% of slot is pink, and significant pink outside badge
        return pink_ratio > 0.15 and non_badge_pink > 10000

    def _detect_badge_presence(self, slot: np.ndarray) -> bool:
        """Detect if slot has a badge (colored rectangle in top-right)."""
        # Badge region is top-right 30x40 of slot
        h, w = slot.shape[:2]
        badge_region = slot[0:40, w-30:w]
        hsv = cv2.cvtColor(badge_region, cv2.COLOR_BGR2HSV)

        # Badge has significant saturation (not gray)
        s_channel = hsv[:, :, 1]
        v_channel = hsv[:, :, 2]

        # Check for colored pixels (S > 30, V > 50)
        colored = cv2.inRange(hsv, np.array([0, 30, 50], np.uint8), np.array([179, 255, 255], np.uint8))
        colored_ratio = colored.sum() / (255 * badge_region.shape[0] * badge_region.shape[1])

        # Also check hue uniformity (badge has consistent hue)
        h_channel = hsv[:, :, 0]
        h_vals = h_channel[colored > 0]
        h_std = h_vals.std() if len(h_vals) > 0 else 255

        # Badge present if >50% colored and hue is somewhat uniform (std < 30)
        return colored_ratio > 0.5 and h_std < 30

    def _read_cost(self, frame: np.ndarray, slot_idx: int) -> tuple[Optional[int], float]:
        """Read cost number (1-5) from the badge.

        Strategy:
        1. Use color threshold (COST_NUM range) to find candidate glyphs
        2. Filter by position (digit is in top-left of badge) and size
        3. If that fails, use background subtraction (median color from non-digit regions)
        """
        box = self.model.cost_badge_box(slot_idx)
        x0, y0, x1, y1 = box
        badge = frame[y0:y1, x0:x1]
        h, w = badge.shape[:2]

        hsv = cv2.cvtColor(badge, cv2.COLOR_BGR2HSV)

        # --- Method 1: Original color threshold with position/size filtering ---
        mask = cv2.inRange(hsv, np.array(COST_NUM_LO, np.uint8), np.array(COST_NUM_HI, np.uint8))
        num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)

        glyphs_found: list[Glyph] = []
        for i in range(1, num):
            x, y, w_comp, h_comp, area = stats[i]
            # Filter by size (font16 digit constraints)
            if not (self.cost_font.min_h <= h_comp <= self.cost_font.max_h):
                continue
            if not (self.cost_font.min_w <= w_comp <= self.cost_font.max_w):
                continue
            if area < self.cost_font.min_area:
                continue
            # Filter by position: digit is in top-left region of badge (roughly y<25, x<25)
            if y >= 25 or x >= 25:
                continue
            glyphs_found.append(Glyph(x + x0, y + y0, w_comp, h_comp, labels[y:y + h_comp, x:x + w_comp] == i))

        if glyphs_found:
            glyphs_found.sort(key=lambda g: g.x)
            templates = self.templates.get("font16", {})
            value, text, score, margin = glyphs.read_number(glyphs_found, templates, self.cost_font)
            if value is not None and 1 <= value <= 5:
                return value, score
            # Try best single digit
            best_digit = None
            best_score = 0.0
            for g in glyphs_found:
                digit, d_score, d_margin = glyphs.classify(g, templates, self.cost_font)
                if digit is not None and digit in "12345" and d_score > best_score:
                    best_digit = int(digit)
                    best_score = d_score
            if best_digit is not None:
                return best_digit, best_score

        # --- Method 2: Background subtraction for solid badges ---
        # Estimate background from non-digit regions (bottom and right of badge)
        # Sample regions: bottom 15px, right 15px (avoiding top-left digit area)
        bg_samples = []
        # Bottom strip
        if h > 25:
            bg_samples.append(hsv[25:, :].reshape(-1, 3))
        # Right strip (excluding top 25px already sampled)
        if w > 25:
            bg_samples.append(hsv[:25, 25:].reshape(-1, 3))
            bg_samples.append(hsv[25:, 25:].reshape(-1, 3))

        if bg_samples:
            bg_pixels = np.vstack(bg_samples)
            # Use median for robustness
            bg_h = int(np.median(bg_pixels[:, 0]))
            bg_s = int(np.median(bg_pixels[:, 1]))
            bg_v = int(np.median(bg_pixels[:, 2]))

            # Create mask for pixels different from background
            h_tol, s_tol, v_tol = 15, 50, 60
            bg_lo = np.array([max(0, bg_h - h_tol), max(0, bg_s - s_tol), max(0, bg_v - v_tol)], np.uint8)
            bg_hi = np.array([min(179, bg_h + h_tol), min(255, bg_s + s_tol), min(255, bg_v + v_tol)], np.uint8)
            bg_mask = cv2.inRange(hsv, bg_lo, bg_hi)
            mask = cv2.bitwise_not(bg_mask)

            # Restrict to digit region (top-left)
            digit_region_mask = np.zeros((h, w), dtype=np.uint8)
            digit_region_mask[0:25, 0:25] = 255
            mask = cv2.bitwise_and(mask, digit_region_mask)

            # Brightness filter
            v_channel = hsv[:, :, 2]
            brightness_mask = cv2.inRange(v_channel, 50, 255)
            mask = cv2.bitwise_and(mask, brightness_mask)

            num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
            glyphs_found = []
            for i in range(1, num):
                x, y, w_comp, h_comp, area = stats[i]
                if not (self.cost_font.min_h <= h_comp <= self.cost_font.max_h):
                    continue
                if not (self.cost_font.min_w <= w_comp <= self.cost_font.max_w):
                    continue
                if area < self.cost_font.min_area:
                    continue
                glyphs_found.append(Glyph(x + x0, y + y0, w_comp, h_comp, labels[y:y + h_comp, x:x + w_comp] == i))

            if glyphs_found:
                glyphs_found.sort(key=lambda g: g.x)
                templates = self.templates.get("font16", {})
                value, text, score, margin = glyphs.read_number(glyphs_found, templates, self.cost_font)
                if value is not None and 1 <= value <= 5:
                    return value, score
                best_digit = None
                best_score = 0.0
                for g in glyphs_found:
                    digit, d_score, d_margin = glyphs.classify(g, templates, self.cost_font)
                    if digit is not None and digit in "12345" and d_score > best_score:
                        best_digit = int(digit)
                        best_score = d_score
                if best_digit is not None:
                    return best_digit, best_score

        return None, 0.0

    def _read_name(self, frame: np.ndarray, slot_idx: int) -> tuple[Optional[str], float]:
        """Read unit name from top-left of slot."""
        box = self.model.name_box(slot_idx)
        x0, y0, x1, y1 = box
        name_region = frame[y0:y1, x0:x1]

        hsv = cv2.cvtColor(name_region, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([0, 0, NAME_V_MIN], np.uint8),
                           np.array([179, NAME_S_MAX, 255], np.uint8))

        num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        glyphs_found: list[Glyph] = []
        for i in range(1, num):
            x, y, w, h, area = stats[i]
            if not (self.font16.min_h <= h <= self.font16.max_h):
                continue
            if not (self.font16.min_w <= w <= self.font16.max_w):
                continue
            if area < self.font16.min_area:
                continue
            glyphs_found.append(Glyph(x + x0, y + y0, w, h, labels[y:y + h, x:x + w] == i))

        if not glyphs_found:
            return None, 0.0

        glyphs_found.sort(key=lambda g: g.x)
        # For names, we'd need letter templates - for now return None
        # TODO: Build letter templates or use OCR for names
        return None, 0.0

    def _read_traits(self, frame: np.ndarray, slot_idx: int) -> tuple[str, ...]:
        """Read trait icons/text from slot. Placeholder for now."""
        return ()


def iter_shop_frames(
    video: str | Path,
    start_ms: int,
    end_ms: int,
    *,
    meta: Optional[VideoMeta] = None,
    model: Optional[ShopModel] = None,
    reader: Optional[ShopReader] = None,
    step: int = 30,
) -> Iterator[tuple[int, int, list[UnitInSlot]]]:
    """Yield (frame_idx, time_ms, shop_slots) every ``step`` frames in a time window.

    The shop is only visible during the planning phase between rounds.
    """
    from .video import iter_frames, probe

    meta = meta or probe(video)
    model = model or ShopModel.for_meta(meta)
    reader = reader or ShopReader(model)

    start_frame = max(0, meta.frame_of_ms(start_ms))
    end_frame = min(meta.frame_count, meta.frame_of_ms(end_ms))
    if end_frame <= start_frame:
        return

    step = max(1, int(step))
    for frame_idx, image in iter_frames(video, meta, start_frame=start_frame, end_frame=end_frame):
        if (frame_idx - start_frame) % step == 0:
            slots = reader.read_shop(image)
            yield frame_idx, meta.ms_of(frame_idx), slots