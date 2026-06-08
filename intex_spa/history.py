"""Temperature history — throttled, retained, JSONL-persisted across restarts.

The supervisor records a sample on each successful poll. To keep the file small
(the poll runs every ~10s), samples are throttled: a new point is stored only when
the temperature changed or `min_interval` seconds elapsed since the last one.
Points older than `retention_hours` are pruned. Persistence is plain JSONL so it
survives LaunchAgent restarts; pass `path=None` for an in-memory-only instance
(used in tests).
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class TempHistory:
    def __init__(
        self,
        path: str | Path | None = "state/history.jsonl",
        retention_hours: float = 168.0,  # 7 days
        min_interval: float = 60.0,
    ) -> None:
        self.path = Path(path) if path else None
        self.retention = retention_hours * 3600
        self.min_interval = min_interval
        self._pts: list[dict] = []
        self._needs_rewrite = False
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # -- public ---------------------------------------------------------------
    def record(
        self,
        current_temp: int | None,
        preset_temp: int | None,
        heater: bool,
        ts: float | None = None,
        air: float | None = None,
        filter_on: bool | None = None,
    ) -> dict | None:
        """Append a sample if it clears the throttle. Returns the point or None.

        `air` is the outside air temp at sample time (from the weather client); it's
        stored when available so the chart can show it and the heat-rate model can
        self-calibrate (loss ∝ water − air).
        """
        if current_temp is None:  # error frame — no reading to plot
            return None
        ts = time.time() if ts is None else ts
        if self._pts:
            last = self._pts[-1]
            if (ts - last["t"]) < self.min_interval and last["cur"] == current_temp:
                return None
        pt = {
            "t": round(float(ts), 1),
            "cur": int(current_temp),
            "set": int(preset_temp) if preset_temp is not None else None,
            "heat": bool(heater),
        }
        if air is not None:
            pt["air"] = round(float(air), 1)
        if filter_on is not None:
            pt["filter"] = bool(filter_on)
        self._pts.append(pt)
        pruned = self._prune(now=ts)
        if self.path:
            if pruned or self._needs_rewrite:
                self._rewrite()
                self._needs_rewrite = False
            else:
                self._append(pt)
        return pt

    def recent(self, hours: float = 24.0, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        cutoff = now - hours * 3600
        return [p for p in self._pts if p["t"] >= cutoff]

    # -- internals ------------------------------------------------------------
    def _prune(self, now: float) -> bool:
        cutoff = now - self.retention
        keep = [p for p in self._pts if p["t"] >= cutoff]
        if len(keep) != len(self._pts):
            self._pts = keep
            return True
        return False

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        pts: list[dict] = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pts.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # tolerate a torn final line
        self._pts = pts
        if self._prune(now=time.time()):
            self._needs_rewrite = True

    def _append(self, pt: dict) -> None:
        # recreate the dir at write time too — it can be removed out from under a
        # long-running process (don't rely solely on the __init__ mkdir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(pt) + "\n")

    def _rewrite(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("".join(json.dumps(p) + "\n" for p in self._pts))
        tmp.replace(self.path)
