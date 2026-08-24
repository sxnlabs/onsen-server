"""Watchdog: notice trouble, text once, text again when it clears.

Written after the outage of 2026-08-22, where the spa dropped off the network at
20:34 in mid-heat and nothing said so for 46 hours: the UI showed a stale-but-
useful panel, `/healthz` answered 200, and the only trace was `network error
(attempt 1):` in the logs.

Shape follows `schedule.py` / `scheduler.py`: `evaluate()` is pure — given a
snapshot, the recent history and a config it returns the conditions currently
firing — and `AlertMonitor` is the async loop that debounces them, sends, and
persists. All the rule logic is unit-testable with no clock, no spa and no SMS
credentials.

Two deliberate choices:

- **One SMS per episode, plus one when it clears.** No re-notification while a
  condition holds: the 46h outage is worth one text at the one-hour mark, not
  46. The persisted state file is what makes that survive a restart — a
  container that reboots mid-outage must not text again.
- **Offline suppresses the spa-side rules.** When the spa is unreachable its
  last status is stale by definition; reporting "water at 32C" off a two-day-old
  frame would be a lie, and would bury the one alert that matters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger("intex_spa.alerts")

UNREACHABLE = "unreachable"
ERROR_CODE = "error_code"
HEATING_STALLED = "heating_stalled"
WATER_LOW = "water_low"


@dataclass(frozen=True)
class AlertConfig:
    """Thresholds. Every delay is "how long the condition must hold before it texts"."""

    unreachable_after: float = 3600.0      # 1h — absorbs a host reboot or a wifi blip
    error_code_after: float = 300.0        # 5 min — E90 can clear on its own
    water_low_c: float | None = 30.0       # None disables the floor check
    water_low_after: float = 900.0
    heating_stall_hours: float = 2.0       # window over which heat must produce a rise
    heating_stall_min_rise: float = 1.0    # degrees C

    def delay_for(self, key: str) -> float:
        """How long the *monitor* holds a condition before texting.

        Zero for the two rules that already carry their own duration —
        `unreachable_after` is measured inside `evaluate()`, and a stall is only
        a stall once the window is covered. Debouncing those twice would turn a
        one-hour promise into two.
        """
        if key == ERROR_CODE:
            return self.error_code_after
        if key == WATER_LOW:
            return self.water_low_after
        return 0.0


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}"


def evaluate(
    *,
    online: bool,
    status: dict | None,
    last_ok_at: float | None,
    samples: list[dict],
    config: AlertConfig,
    now: float,
) -> dict[str, str]:
    """Return {condition key: SMS body} for every condition firing right now.

    Pure. `samples` are `TempHistory` points (newest last); `last_ok_at` is when
    the spa last answered — from the supervisor, or the newest sample after a
    restart, or process start on a cold boot that never reached the spa.
    """
    firing: dict[str, str] = {}

    if not online:
        if last_ok_at is None:
            return firing
        elapsed = now - last_ok_at
        if elapsed >= config.unreachable_after:
            last = f", eau {status['current_temp']}C" if status and status.get("current_temp") is not None else ""
            firing[UNREACHABLE] = (
                f"Onsen: spa injoignable depuis {_duration(elapsed)}. "
                f"Derniere donnee {_hhmm(last_ok_at)}{last}. Verifier l'alimentation du spa."
            )
        # A stale frame says nothing true about the water — don't judge it.
        return firing

    if not status:
        return firing

    code = status.get("error_code")
    if code:
        firing[ERROR_CODE] = (
            f"Onsen: le spa signale l'erreur {code}. "
            f"Eau {status.get('current_temp')}C, consigne {status.get('preset_temp')}C."
        )

    stall = _heating_stalled(samples=samples, config=config, now=now)
    if stall is not None:
        rise, hours = stall
        firing[HEATING_STALLED] = (
            f"Onsen: chauffe active depuis {hours:g}h pour +{rise:g}C seulement "
            f"(eau {status.get('current_temp')}C, consigne {status.get('preset_temp')}C). "
            "Resistance ou couvercle ?"
        )

    current = status.get("current_temp")
    preset = status.get("preset_temp")
    if (
        config.water_low_c is not None
        and current is not None
        and current <= config.water_low_c
        and preset is not None
        and preset > current
    ):
        firing[WATER_LOW] = (
            f"Onsen: eau a {current}C, sous le seuil de {config.water_low_c:g}C "
            f"(consigne {preset}C)."
        )

    return firing


def _heating_stalled(
    *, samples: list[dict], config: AlertConfig, now: float
) -> tuple[float, float] | None:
    """(rise, hours) when the heater ran the whole window without earning its keep.

    Requires the window to be *covered* — a restart, or a spa that just came back,
    leaves two samples spanning ten minutes, and calling that a stalled two-hour
    heat cycle would text on every recovery.
    """
    window_seconds = config.heating_stall_hours * 3600
    if window_seconds <= 0:
        return None
    window = [p for p in samples if p["t"] >= now - window_seconds]
    if len(window) < 2:
        return None
    # Tolerate one missed sample at the edge (history throttles at 60s).
    if window[-1]["t"] - window[0]["t"] < window_seconds - 120:
        return None
    if not all(p.get("heat") for p in window):
        return None
    start = window[0]
    if start.get("set") is None or start["set"] <= start["cur"]:
        return None  # already at setpoint: the heater has nothing to prove
    rise = max(p["cur"] for p in window) - start["cur"]
    if rise >= config.heating_stall_min_rise:
        return None
    return float(rise), config.heating_stall_hours


_RESOLVED = {
    UNREACHABLE: "spa de nouveau joignable",
    ERROR_CODE: "erreur disparue",
    HEATING_STALLED: "la chauffe repart",
    WATER_LOW: "temperature revenue au-dessus du seuil",
}


def resolution_message(key: str, status: dict | None) -> str:
    label = _RESOLVED.get(key, "retour a la normale")
    water = ""
    if status and status.get("current_temp") is not None:
        water = f" Eau {status['current_temp']}C, consigne {status.get('preset_temp')}C."
    return f"Onsen: {label}.{water}"


class AlertMonitor:
    """Debounces `evaluate()`, texts once per episode, texts again on clear.

    Holds no spa connection of its own (invariant: one client, one supervisor) —
    it reads the supervisor's published state and the history the poll loop
    already writes.
    """

    def __init__(
        self,
        supervisor,
        sender,
        *,
        config: AlertConfig | None = None,
        state_path: str | Path | None = "state/alerts.json",
        interval: float = 60.0,
    ) -> None:
        self.supervisor = supervisor
        self.sender = sender
        self.config = config or AlertConfig()
        self.state_path = Path(state_path) if state_path else None
        self.interval = interval
        self.started_at = time.time()
        self._episodes: dict[str, dict] = self._load()
        self._task: asyncio.Task | None = None

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — alerting must never kill itself
                _LOG.exception("alert tick failed (non-fatal)")

    # -- the tick -------------------------------------------------------------
    async def tick(self, now: float | None = None) -> dict[str, dict]:
        now = time.time() if now is None else now
        state = self.supervisor.state
        status = state.get("status")
        samples = self.supervisor.history.recent(
            hours=max(self.config.heating_stall_hours * 2, 3.0), now=now
        )
        firing = evaluate(
            online=bool(state.get("online")),
            status=status,
            last_ok_at=self._last_ok_at(samples),
            samples=samples,
            config=self.config,
            now=now,
        )

        changed = False
        for key, message in firing.items():
            episode = self._episodes.get(key)
            if episode is None:
                episode = {"since": now, "notified_at": None}
                self._episodes[key] = episode
                changed = True
            if episode["notified_at"] is None and now - episode["since"] >= self.config.delay_for(key):
                # A failed send leaves notified_at None: the next tick retries
                # rather than losing the alert to a transient OVH error.
                if await self.sender.send(message):
                    episode["notified_at"] = now
                    changed = True

        for key in [k for k in self._episodes if k not in firing]:
            episode = self._episodes.pop(key)
            changed = True
            if episode.get("notified_at") is not None:
                await self.sender.send(resolution_message(key, status))

        if changed:
            self._save()
        return dict(self._episodes)

    def snapshot(self) -> dict:
        return {
            "config": vars(self.config),
            "episodes": {
                key: {
                    "since": episode["since"],
                    "notified": episode.get("notified_at") is not None,
                }
                for key, episode in self._episodes.items()
            },
        }

    # -- internals ------------------------------------------------------------
    def _last_ok_at(self, samples: list[dict]) -> float | None:
        """When the spa last answered.

        `supervisor.last_ok_at` is the truth while the process lives. After a
        restart mid-outage it's None, and the newest history sample is the next
        best witness — without it a reboot would reset the one-hour clock and an
        outage across a redeploy would never text. A cold start that has never
        reached the spa falls back to process start.
        """
        if getattr(self.supervisor, "last_ok_at", None) is not None:
            return self.supervisor.last_ok_at
        if samples:
            return samples[-1]["t"]
        return self.started_at

    def _load(self) -> dict[str, dict]:
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            _LOG.warning("failed to read alert state", exc_info=True)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            key: value
            for key, value in raw.items()
            if isinstance(value, dict) and isinstance(value.get("since"), (int, float))
        }

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._episodes))
            tmp.replace(self.state_path)
        except OSError:
            _LOG.warning("failed to persist alert state", exc_info=True)
