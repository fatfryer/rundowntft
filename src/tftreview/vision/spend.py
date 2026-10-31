"""Gold spend detection and classification.

Analyzes shop slot changes between frames to classify gold spending actions:
- roll: 5 slots changed simultaneously
- unit: 1 slot cleared (bought)
- charm: pink card slot cleared
- xp: no shop changes (requires gold correlation)
- sell: unit removed from board (not from shop), gold +cost (handled separately)

Since the gold HUD is not readable during the planning phase, this module
primarily detects spends from shop changes alone. Gold correlation is
attempted when gold readings become available later in the round.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .shop import ShopReader, UnitInSlot, ShopModel
from .metrics import MetricsReader


@dataclass(frozen=True)
class SpendAction:
    """A detected gold spend action."""
    kind: str              # "roll", "unit", "charm", "xp", "unknown"
    cost: int              # gold cost of action (estimated from unit cost for unit/charm)
    gold_before: Optional[int] = None
    gold_after: Optional[int] = None
    unit_cost: Optional[int] = None    # for unit/charm: cost of unit bought
    unit_is_charm: bool = False        # for unit: was it a charm?
    slot_idx: Optional[int] = None     # which slot changed (for unit/charm)
    confidence: float = 0.0
    frame_idx: int = 0
    time_ms: int = 0
    meta: dict = None  # additional info

    def __post_init__(self):
        if self.meta is None:
            object.__setattr__(self, 'meta', {})


class SpendTracker:
    """Tracks shop state across frames to detect gold spends from shop changes."""

    def __init__(self, shop_reader: ShopReader, metrics_reader: MetricsReader = None, confirm_frames: int = 3, stable_frames: int = 2):
        self.shop_reader = shop_reader
        self.metrics_reader = metrics_reader
        self.prev_stable_slots: list[UnitInSlot] = []  # temporally smoothed slot states
        self.raw_slot_history: list[list[UnitInSlot]] = []  # recent raw detections
        self.prev_gold: Optional[int] = None
        self.pending_changes: list[dict] = []  # shop changes waiting for gold
        self.frame_count = 0
        self.confirm_frames = confirm_frames
        self.stable_frames = stable_frames
        # For temporal confirmation of changes
        self.potential_clears: dict[int, dict] = {}
        self.potential_appears: dict[int, dict] = {}
        self.potential_changes: dict[int, dict] = {}

    def _update_stable_slots(self, current_raw: list[UnitInSlot]) -> list[UnitInSlot]:
        """Update stable slot states using temporal smoothing."""
        self.raw_slot_history.append(current_raw)
        if len(self.raw_slot_history) > self.stable_frames:
            self.raw_slot_history.pop(0)

        # Compute stable state: a slot is "occupied" if it has a cost in at least
        # (stable_frames - 1) of the recent frames
        n_slots = len(current_raw)
        stable_slots = []

        for i in range(n_slots):
            # Count frames where this slot has a cost
            cost_counts = {}
            for hist in self.raw_slot_history:
                unit = hist[i]
                if unit.cost is not None:
                    cost_counts[unit.cost] = cost_counts.get(unit.cost, 0) + 1

            if cost_counts:
                # Most common cost in recent frames
                best_cost = max(cost_counts, key=cost_counts.get)
                if cost_counts[best_cost] >= self.stable_frames - 1:
                    # Slot is stably occupied with this cost
                    # Use the most recent unit with this cost
                    for hist in reversed(self.raw_slot_history):
                        if hist[i].cost == best_cost:
                            stable_slots.append(hist[i])
                            break
                    continue

            # Slot is stably empty
            stable_slots.append(UnitInSlot())

        return stable_slots

    def feed(self, frame: np.ndarray, frame_idx: int, time_ms: int) -> list[SpendAction]:
        """Process a frame, return any detected spend actions."""
        self.frame_count += 1
        current_raw = self.shop_reader.read_shop(frame)
        gold = None
        if self.metrics_reader:
            metrics = self.metrics_reader.read(frame)
            gold = metrics.gold

        # Update stable slot states
        current_slots = self._update_stable_slots(current_raw)

        actions = []

        # Detect shop changes with temporal confirmation
        if self.prev_stable_slots:
            confirmed = self._update_temporal_confirmation(current_slots, frame_idx, time_ms)

            # Classify confirmed changes based on shop state alone
            for change in confirmed:
                action = self._classify_from_shop_change(change)
                if action:
                    actions.append(action)

                # Also store for potential gold correlation
                change['gold_at_change'] = gold
                self.pending_changes.append(change)

        # If we got a new gold reading, try to resolve/confirm pending changes
        if gold is not None and gold != self.prev_gold and self.prev_gold is not None:
            resolved = self._resolve_pending_with_gold_delta(self.prev_gold, gold)
            actions.extend(resolved)

        self.prev_stable_slots = current_slots
        if gold is not None:
            self.prev_gold = gold

        return actions

    def _update_temporal_confirmation(self, current_slots: list[UnitInSlot], frame_idx: int, time_ms: int) -> list[dict]:
        """Update temporal confirmation state and return newly confirmed changes."""
        confirmed = []

        for i, (prev, curr) in enumerate(zip(self.prev_stable_slots, current_slots)):
            prev_empty = prev.is_empty()
            curr_empty = curr.is_empty()

            if prev_empty and curr_empty:
                # Both empty - clear any pending for this slot
                self.potential_clears.pop(i, None)
                self.potential_appears.pop(i, None)
                self.potential_changes.pop(i, None)
                continue

            if not prev_empty and curr_empty:
                # Slot cleared - unit potentially bought
                if i in self.potential_clears:
                    self.potential_clears[i]['count'] += 1
                else:
                    self.potential_clears[i] = {
                        'unit': prev,
                        'count': 1,
                        'first_frame': frame_idx,
                        'first_time': time_ms,
                        'slot_idx': i,
                    }
                # Clear other potentials for this slot
                self.potential_appears.pop(i, None)
                self.potential_changes.pop(i, None)

                # Check if confirmed
                if self.potential_clears[i]['count'] >= self.confirm_frames:
                    confirmed.append({
                        'type': 'cleared',
                        'slot_idx': i,
                        'prev_unit': self.potential_clears[i]['unit'],
                        'frame_idx': self.potential_clears[i]['first_frame'],
                        'time_ms': self.potential_clears[i]['first_time'],
                    })
                    del self.potential_clears[i]

            elif prev_empty and not curr_empty:
                # New unit appeared (reroll)
                if i in self.potential_appears:
                    self.potential_appears[i]['count'] += 1
                else:
                    self.potential_appears[i] = {
                        'unit': curr,
                        'count': 1,
                        'first_frame': frame_idx,
                        'first_time': time_ms,
                        'slot_idx': i,
                    }
                self.potential_clears.pop(i, None)
                self.potential_changes.pop(i, None)

                if self.potential_appears[i]['count'] >= self.confirm_frames:
                    confirmed.append({
                        'type': 'appeared',
                        'slot_idx': i,
                        'curr_unit': self.potential_appears[i]['unit'],
                        'frame_idx': self.potential_appears[i]['first_frame'],
                        'time_ms': self.potential_appears[i]['first_time'],
                    })
                    del self.potential_appears[i]

            elif prev.cost != curr.cost or prev.is_charm != curr.is_charm:
                # Unit changed (reroll)
                if i in self.potential_changes:
                    self.potential_changes[i]['count'] += 1
                else:
                    self.potential_changes[i] = {
                        'prev_unit': prev,
                        'curr_unit': curr,
                        'count': 1,
                        'first_frame': frame_idx,
                        'first_time': time_ms,
                        'slot_idx': i,
                    }
                self.potential_clears.pop(i, None)
                self.potential_appears.pop(i, None)

                if self.potential_changes[i]['count'] >= self.confirm_frames:
                    confirmed.append({
                        'type': 'changed',
                        'slot_idx': i,
                        'prev_unit': self.potential_changes[i]['prev_unit'],
                        'curr_unit': self.potential_changes[i]['curr_unit'],
                        'frame_idx': self.potential_changes[i]['first_frame'],
                        'time_ms': self.potential_changes[i]['first_time'],
                    })
                    del self.potential_changes[i]

            else:
                # No change - clear potentials for this slot
                self.potential_clears.pop(i, None)
                self.potential_appears.pop(i, None)
                self.potential_changes.pop(i, None)

        return confirmed

    def _classify_from_shop_change(self, change: dict) -> Optional[SpendAction]:
        """Classify a shop change based on shop state alone (no gold)."""
        change_type = change['type']
        slot_idx = change['slot_idx']
        frame_idx = change['frame_idx']
        time_ms = change['time_ms']

        if change_type == 'cleared':
            prev_unit = change['prev_unit']
            unit_cost = prev_unit.cost
            if unit_cost:
                return SpendAction(
                    kind="charm" if prev_unit.is_charm else "unit",
                    cost=unit_cost,  # estimated from unit cost
                    gold_before=None,
                    gold_after=None,
                    unit_cost=unit_cost,
                    unit_is_charm=prev_unit.is_charm,
                    slot_idx=slot_idx,
                    confidence=0.70,  # lower without gold confirmation
                    frame_idx=frame_idx,
                    time_ms=time_ms,
                    meta={'detection': 'shop_change_only'}
                )

        return None

    def _resolve_pending_with_gold_delta(self, gold_before: int, gold_after: int) -> list[SpendAction]:
        """Resolve pending shop changes using a gold delta (for confirmation)."""
        actions = []
        gold_delta = gold_after - gold_before
        if gold_delta >= 0 or not self.pending_changes:
            return actions

        gold_spent = -gold_delta

        # Group pending changes by type
        cleared = [c for c in self.pending_changes if c['type'] == 'cleared']
        appeared = [c for c in self.pending_changes if c['type'] == 'appeared']
        changed = [c for c in self.pending_changes if c['type'] == 'changed']

        n_cleared = len(cleared)
        n_appeared = len(appeared)
        n_changed = len(changed)
        n_total_changes = n_cleared + n_appeared + n_changed

        # Roll: 5 slots appeared/changed, gold -2
        if n_total_changes == 5 and n_cleared == 0 and gold_spent == 2:
            actions.append(SpendAction(
                kind="roll",
                cost=2,
                gold_before=gold_before,
                gold_after=gold_after,
                confidence=0.95,
                frame_idx=self.pending_changes[0]['frame_idx'],
                time_ms=self.pending_changes[0]['time_ms'],
                meta={'detection': 'shop_change_with_gold', 'gold_spent': 2}
            ))
            self.pending_changes.clear()
            return actions

        # XP: no shop changes, gold -4
        if n_total_changes == 0 and gold_spent == 4:
            actions.append(SpendAction(
                kind="xp",
                cost=4,
                gold_before=gold_before,
                gold_after=gold_after,
                confidence=0.90,
                frame_idx=self.frame_count,
                time_ms=self.pending_changes[-1]['time_ms'] if self.pending_changes else 0,
                meta={'detection': 'gold_only', 'gold_spent': 4}
            ))
            return actions

        # Unit purchase: 1 cleared, gold matches unit cost
        if n_cleared == 1 and n_appeared == 0 and n_changed == 0:
            change = cleared[0]
            prev_unit = change['prev_unit']
            unit_cost = prev_unit.cost
            if unit_cost and gold_spent == unit_cost:
                actions.append(SpendAction(
                    kind="charm" if prev_unit.is_charm else "unit",
                    cost=gold_spent,
                    gold_before=gold_before,
                    gold_after=gold_after,
                    unit_cost=unit_cost,
                    unit_is_charm=prev_unit.is_charm,
                    slot_idx=change['slot_idx'],
                    confidence=0.95,
                    frame_idx=change['frame_idx'],
                    time_ms=change['time_ms'],
                    meta={'detection': 'shop_change_with_gold', 'gold_spent': gold_spent}
                ))
                self.pending_changes.clear()
                return actions

        # Charm purchase: 1 cleared charm, gold matches
        if n_cleared == 1 and n_appeared == 0 and n_changed == 0:
            change = cleared[0]
            if change['prev_unit'].is_charm:
                unit_cost = change['prev_unit'].cost
                if unit_cost and gold_spent == unit_cost:
                    actions.append(SpendAction(
                        kind="charm",
                        cost=gold_spent,
                        gold_before=gold_before,
                        gold_after=gold_after,
                        unit_cost=unit_cost,
                        unit_is_charm=True,
                        slot_idx=change['slot_idx'],
                        confidence=0.95,
                        frame_idx=change['frame_idx'],
                        time_ms=change['time_ms'],
                        meta={'detection': 'shop_change_with_gold', 'gold_spent': gold_spent}
                    ))
                    self.pending_changes.clear()
                    return actions

        # If gold changed but we can't classify, record as unknown
        if gold_spent > 0:
            actions.append(SpendAction(
                kind="unknown",
                cost=gold_spent,
                gold_before=gold_before,
                gold_after=gold_after,
                confidence=0.0,
                frame_idx=self.pending_changes[-1]['frame_idx'] if self.pending_changes else self.frame_count,
                time_ms=self.pending_changes[-1]['time_ms'] if self.pending_changes else 0,
                meta={'detection': 'gold_only', 'gold_spent': gold_spent, 'pending_shop_changes': n_total_changes}
            ))

        # Limit pending to avoid memory buildup
        if len(self.pending_changes) > 50:
            self.pending_changes = self.pending_changes[-25:]

        return actions


def scan_spends(
    video: str,
    start_ms: int,
    end_ms: int,
    *,
    shop_reader: ShopReader,
    metrics_reader: MetricsReader = None,
    step: int = 1,
    confirm_frames: int = 3,
    stable_frames: int = 2,
) -> list[tuple[int, int, list[SpendAction]]]:
    """Scan a video time window for spend actions.

    Returns list of (frame_idx, time_ms, actions).
    """
    from .video import iter_frames, probe

    meta = probe(video)
    start_frame = max(0, meta.frame_of_ms(start_ms))
    end_frame = min(meta.frame_count, meta.frame_of_ms(end_ms))
    if end_frame <= start_frame:
        return []

    tracker = SpendTracker(shop_reader, metrics_reader, confirm_frames=confirm_frames, stable_frames=stable_frames)
    results = []

    for frame_idx, image in iter_frames(video, meta, start_frame=start_frame, end_frame=end_frame):
        if (frame_idx - start_frame) % step != 0:
            continue

        time_ms = meta.ms_of(frame_idx)
        actions = tracker.feed(image, frame_idx, time_ms)
        if actions:
            results.append((frame_idx, time_ms, actions))

    return results