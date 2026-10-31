"""Time formatting/parsing helpers. All internal times are integer milliseconds."""
from __future__ import annotations


def format_time(ms: float, with_ms: bool = False) -> str:
    ms = max(0, int(ms))
    total_s, rem = divmod(ms, 1000)
    h, rem_s = divmod(total_s, 3600)
    m, s = divmod(rem_s, 60)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return f"{base}.{rem:03d}" if with_ms else base


def parse_time(text: str) -> int:
    """Parse ``1:23:45``, ``83:12``, ``723`` (seconds) or ``723000ms`` into milliseconds."""
    t = text.strip().lower()
    if not t:
        raise ValueError("empty time")
    if t.endswith("ms"):
        value = int(float(t[:-2]))
    elif ":" in t:
        parts = t.split(":")
        if len(parts) > 3:
            raise ValueError(f"bad time: {text!r}")
        secs = 0.0
        for part in parts:
            secs = secs * 60 + float(part)
        value = int(round(secs * 1000))
    else:
        value = int(round(float(t) * 1000))
    if value < 0:
        raise ValueError("time cannot be negative")
    return value
