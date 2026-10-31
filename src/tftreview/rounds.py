"""The expected sequence of rounds in a ranked TFT game.

This is *data*, not logic: ``RoundConfig`` describes how many rounds each stage has
and which of them are carousel / PvE. Set changes, or any place where the default
doesn't match the game, are fixed by dropping a ``rounds.json`` in the app data folder
(see ``tftreview rounds``) rather than by editing code.

NOTE: the default below is a best guess at the current ranked layout (carousel at 1-1
and x-4, PvE at 1-2..1-4 and x-7). Verify it against your own VODs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

LABEL_RE = re.compile(r"^(\d+)-(\d+)$")
PVP, PVE, CAROUSEL = "pvp", "pve", "carousel"


def parse_label(label: str) -> Optional[tuple[int, int]]:
    """``"3-2"`` -> ``(3, 2)``; anything that isn't stage-round -> None."""
    m = LABEL_RE.match(label.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


@dataclass(frozen=True)
class RoundSlot:
    label: str
    stage: int
    round: int
    round_type: str


@dataclass
class RoundConfig:
    name: str
    stages: dict[int, int]                      # stage -> number of rounds
    default_type: str = PVP
    overrides: dict[str, str] = field(default_factory=dict)  # "1-1" or wildcard "*-4"

    def type_for(self, label: str) -> str:
        if label in self.overrides:
            return self.overrides[label]
        parsed = parse_label(label)
        if parsed:
            wildcard = f"*-{parsed[1]}"
            if wildcard in self.overrides:
                return self.overrides[wildcard]
        return self.default_type

    def slots(self) -> list[RoundSlot]:
        out = []
        for stage in sorted(self.stages):
            for rnd in range(1, self.stages[stage] + 1):
                label = f"{stage}-{rnd}"
                out.append(RoundSlot(label, stage, rnd, self.type_for(label)))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "stages": {str(k): v for k, v in sorted(self.stages.items())},
            "default_type": self.default_type,
            "overrides": dict(self.overrides),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoundConfig":
        return cls(
            name=data.get("name", "custom"),
            stages={int(k): int(v) for k, v in data["stages"].items()},
            default_type=data.get("default_type", PVP),
            overrides=dict(data.get("overrides", {})),
        )

    @classmethod
    def load(cls, path: str | Path) -> "RoundConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def default_config() -> RoundConfig:
    return RoundConfig(
        name="ranked-default",
        stages={1: 4, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 7},
        default_type=PVP,
        overrides={
            "1-1": CAROUSEL,
            "1-2": PVE, "1-3": PVE, "1-4": PVE,
            "*-4": CAROUSEL,
            "*-7": PVE,
        },
    )


def load_config_or_default(path: str | Path) -> RoundConfig:
    p = Path(path)
    if p.exists():
        try:
            return RoundConfig.load(p)
        except (ValueError, KeyError, TypeError):
            pass  # fall back to the default rather than refusing to start
    return default_config()


class RoundSequence:
    """Ordered round slots with cursor helpers."""

    def __init__(self, config: RoundConfig):
        self.config = config
        self.slots = config.slots()
        self._index = {s.label: i for i, s in enumerate(self.slots)}

    def index(self, label: str) -> Optional[int]:
        return self._index.get(label)

    def slot(self, label: str) -> RoundSlot:
        """Slot for a label. Labels outside the config are fine as long as they look like ``S-R``."""
        i = self._index.get(label)
        if i is not None:
            return self.slots[i]
        parsed = parse_label(label)
        if parsed is None:
            raise ValueError(f"not a round label (expected like '2-1'): {label!r}")
        return RoundSlot(label, parsed[0], parsed[1], self.config.type_for(label))

    def after(self, label: Optional[str]) -> Optional[RoundSlot]:
        """The slot after ``label``; the very first slot when ``label`` is None."""
        if label is None:
            return self.slots[0] if self.slots else None
        i = self._index.get(label)
        if i is None or i + 1 >= len(self.slots):
            return None
        return self.slots[i + 1]
