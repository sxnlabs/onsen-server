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
- **Only faults text.** SMS cost real money, so a text has to mean something is
  broken: the spa is gone, the spa says it's broken, or the heater is running
  for nothing. The water floor is none of those — a spa at 29C climbing to 37C
  is a normal cold start, and the rule fired on every one of them. It's kept,
  off by default, for anyone who wants a freeze warning (`water_low_c`).
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
    # Off by default: not a fault, just a cold spa. Set a temperature to arm it
    # (a freeze warning is the use case) — 30.0 was the old always-on value.
    water_low_c: float | None = None
    water_low_after: float = 900.0
    heating_stall_hours: float = 2.0       # window over which heat must produce a rise
    heating_stall_min_rise: float = 1.0    # degrees C
    # History records a point on change or every 60s, and only on a *successful*
    # poll. A hole wider than this means we weren't watching — the heater could
    # have been anything in between, so the window can't be judged.
    sample_max_gap: float = 600.0

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

    def disabled_keys(self) -> set[str]:
        """Rules this config switches off entirely.

        `AlertMonitor` drops their open episodes at load rather than letting
        them clear: turning a rule off is not an observation, and the owner who
        just silenced the water floor should not get one last "alerte levee".
        """
        disabled = set()
        if self.water_low_c is None:
            disabled.add(WATER_LOW)
        if self.heating_stall_hours <= 0:
            disabled.add(HEATING_STALLED)
        return disabled


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
        # An error frame carries no reading: `decode_status` sets current_temp to
        # None precisely when it sets error_code. Saying "Eau NoneC" in the one
        # SMS about a sensor fault would be a bad joke.
        water = (
            f" Eau {status['current_temp']}C, consigne {status.get('preset_temp')}C."
            if status.get("current_temp") is not None
            else f" Consigne {status.get('preset_temp')}C."
        )
        firing[ERROR_CODE] = f"Onsen: le spa signale l'erreur {code}.{water}"

    current = status.get("current_temp")
    preset = status.get("preset_temp")

    # A stall is only a stall on a frame that is both readable and still asking
    # for heat. The live frame outranks the window on all three counts:
    # `TempHistory.record()` used to drop a heater-off observation inside the
    # throttle; an error frame carries no reading at all (and would format "eau
    # NoneC" off pre-fault samples); and a setpoint lowered mid-window means the
    # thermostat is right to stop climbing — "eau 30C, consigne 30C" is not a
    # fault, it's an arrival.
    heating_requested = (
        status.get("heater")
        and not code
        and current is not None
        and preset is not None
        and preset > current
    )
    stall = (
        _heating_stalled(samples=samples, config=config, now=now)
        if heating_requested
        else None
    )
    if stall is not None:
        rise, hours = stall
        firing[HEATING_STALLED] = (
            f"Onsen: chauffe active depuis {hours:g}h pour +{rise:g}C seulement "
            f"(eau {current}C, consigne {preset}C). "
            "Resistance ou couvercle ?"
        )

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
    # Covered is not the same as continuous: two heater-on samples two hours
    # apart, either side of a polling outage, would otherwise read as two hours
    # of stalled heat. We only know what we sampled.
    if any(b["t"] - a["t"] > config.sample_max_gap for a, b in zip(window, window[1:])):
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


# How stale the persisted "the spa answered" witness may get. It exists only to
# survive a restart, so a coarse resolution is enough — and this is one small
# write every ten minutes, not one per tick.
WITNESS_INTERVAL = 600.0

# Only say what was actually observed. An episode stops firing for more reasons
# than the happy one: a stall clears when the heater is switched off or the
# window goes inconclusive, and a water-low clears when the setpoint is lowered
# under water that is still cold. "La chauffe repart" would then be the opposite
# of the truth, so those two get a neutral label and let the reading that
# follows speak. The other two *are* the observation: an online frame, and an
# online frame with no error code.
_RESOLVED = {
    UNREACHABLE: "spa de nouveau joignable",
    ERROR_CODE: "plus d'erreur signalee",
    HEATING_STALLED: "alerte chauffe levee",
    WATER_LOW: "alerte eau basse levee",
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
        self._episodes, self._last_ok_witness = self._load()
        # Persist the purge immediately. Leaving it in memory only would let the
        # episode outlive a restart on disk, and a floor armed again months later
        # (a freeze warning, say) would reload it: `evaluate()` wouldn't fire it,
        # the clearing branch would text "alerte eau basse levee" out of nowhere,
        # and the `notified_at` it carries would swallow the first real one.
        stale = self.config.disabled_keys() & self._episodes.keys()
        if stale:
            for key in stale:
                self._episodes.pop(key)
            self._save()
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
        online = bool(state.get("online"))
        # Wide enough for the stall window *and* for `_last_ok_at` to still find
        # the last good sample after a restart, however long the outage delay is
        # configured to be.
        samples = self.supervisor.history.recent(
            hours=max(
                self.config.heating_stall_hours * 2,
                self.config.unreachable_after / 3600 + 1,
                3.0,
            ),
            now=now,
        )
        firing = evaluate(
            online=online,
            status=status,
            last_ok_at=self._last_ok_at(samples),
            samples=samples,
            config=self.config,
            now=now,
        )

        changed = self._witness(self.supervisor.last_ok_at)
        for key, message in firing.items():
            episode = self._episodes.get(key)
            if episode is None:
                episode = {"since": now, "notified_at": None}
                self._episodes[key] = episode
                changed = True
            elif episode.pop("cleared_at", None) is not None:
                changed = True  # it came back before we could announce it was over
            if key == ERROR_CODE:
                # A different fault is a different alert. Without this, a spa
                # going straight from E90 to E94 reuses the notified episode and
                # the new (possibly worse) diagnosis is never sent — it would
                # wait for an error-free frame to close the first one.
                code = (status or {}).get("error_code")
                if episode.get("code") not in (None, code):
                    episode.update(since=now, notified_at=None, code=code)
                    changed = True
                elif episode.get("code") != code:
                    episode["code"] = code
                    changed = True
            if episode["notified_at"] is None and now - episode["since"] >= self.config.delay_for(key):
                # A failed send leaves notified_at None: the next tick retries
                # rather than losing the alert to a transient OVH error.
                if await self.sender.send(message):
                    episode["notified_at"] = now
                    changed = True

        for key in [k for k in self._episodes if k not in firing]:
            episode = self._episodes[key]
            if episode.get("notified_at") is None:
                # Nothing was ever said, so there is nothing to take back — and
                # an interrupted debounce is not a debounce: a water-low
                # condition seen for five minutes before the link died has not
                # persisted fifteen. If it's still there when the spa comes
                # back, its clock starts again.
                self._episodes.pop(key)
                changed = True
                continue
            # Nothing is announced as over while the spa is unreachable. For a
            # spa-side rule `evaluate()` deliberately says nothing, and reading
            # that silence as recovery would text "erreur disparue" off a stale
            # frame, right after an E90. And an outage that reconnects, fails to
            # send its resolution, then drops again below the threshold is not
            # "de nouveau joignable" either — it just isn't firing yet.
            if not online:
                continue
            changed = True
            # Owe a resolution SMS: keep the episode until it actually goes out,
            # same retry contract as the opening alert.
            episode.setdefault("cleared_at", now)
            if await self.sender.send(resolution_message(key, status)):
                self._episodes.pop(key)

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
                    "clearing": episode.get("cleared_at") is not None,
                }
                for key, episode in self._episodes.items()
            },
        }

    # -- internals ------------------------------------------------------------
    def _witness(self, last_ok_at: float | None) -> bool:
        """Remember, coarsely, that the spa answered. True when it moved enough to save."""
        if last_ok_at is None:
            return False
        if self._last_ok_witness is not None and last_ok_at - self._last_ok_witness < WITNESS_INTERVAL:
            return False
        self._last_ok_witness = last_ok_at
        return True

    def _last_ok_at(self, samples: list[dict]) -> float | None:
        """When the spa last answered.

        `supervisor.last_ok_at` is the truth while the process lives. After a
        restart mid-outage it's None — without a fallback a reboot would reset
        the one-hour clock and an outage across a redeploy would never text.

        The persisted witness leads the newest history sample because a spa
        answering with an *error* frame is a successful reply that writes no
        history point (`decode_status` sets current_temp=None with the error
        code): during a sustained E90 the temp series stops entirely, and
        trusting it alone would date the outage hours too early.
        """
        if getattr(self.supervisor, "last_ok_at", None) is not None:
            return self.supervisor.last_ok_at
        candidates = [t for t in (self._last_ok_witness, samples[-1]["t"] if samples else None) if t]
        return max(candidates) if candidates else self.started_at

    def _load(self) -> tuple[dict[str, dict], float | None]:
        if self.state_path is None or not self.state_path.exists():
            return {}, None
        try:
            raw = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            _LOG.warning("failed to read alert state", exc_info=True)
            return {}, None
        if not isinstance(raw, dict):
            return {}, None
        episodes = raw.get("episodes") if isinstance(raw.get("episodes"), dict) else raw
        witness = raw.get("last_ok_at")
        return (
            {
                key: value
                for key, value in episodes.items()
                if isinstance(value, dict) and isinstance(value.get("since"), (int, float))
            },
            witness if isinstance(witness, (int, float)) else None,
        )

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"episodes": self._episodes, "last_ok_at": self._last_ok_witness})
            )
            tmp.replace(self.state_path)
        except OSError:
            _LOG.warning("failed to persist alert state", exc_info=True)
