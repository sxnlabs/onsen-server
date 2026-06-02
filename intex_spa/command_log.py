"""Append-only audit log for commands sent to the spa.

This is deliberately tiny and fail-soft: command logging must never prevent a
manual safety action from reaching the spa.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class CommandLog:
    def __init__(self, path: str | Path | None = "state/commands.jsonl") -> None:
        self.path = Path(path) if path else None

    def record(self, **entry) -> None:
        if self.path is None:
            return
        row = {"t": round(time.time(), 3), **entry}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
