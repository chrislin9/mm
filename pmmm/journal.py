"""Append-only JSONL journal. The weekly loss anchor lives here, so restarting the bot can
never reset the week's budget unless the operator explicitly passes --init-week."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CRITICAL = {"anchor", "halt", "fill", "inflows"}


def week_of(ts: float) -> tuple[str, float]:
    """(ISO week id, epoch of Monday 00:00 America/New_York) for the week containing ts."""
    dt = datetime.fromtimestamp(ts, ET)
    monday = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    y, w, _ = monday.isocalendar()
    return f"{y}-W{w:02d}", monday.timestamp()


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **data) -> None:
        rec = {"ts": round(time.time(), 3), "kind": kind, **data}
        line = json.dumps(rec, default=str, separators=(",", ":")) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            if kind in CRITICAL:
                f.flush()
                os.fsync(f.fileno())

    def _records(self, kind: str):
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("kind") == kind:
                    yield rec

    def find_anchor(self, week_id: str) -> dict | None:
        last = None
        for rec in self._records("anchor"):
            if rec.get("week") == week_id:
                last = rec
        return last

    def has_any_anchor(self) -> bool:
        return any(True for _ in self._records("anchor"))
