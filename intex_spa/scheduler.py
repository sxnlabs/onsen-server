"""Async scheduler: turns the decision engine into actions on the spa.

Each tick it gathers inputs (last known status, today's Tempo color, the learned
heat rate), asks `schedule.evaluate()` for the desired state, and reconciles the spa
toward it via the supervisor (whose sets are idempotent read-before-write). User
actions in the UI register a short-lived per-field manual override so the scheduler
doesn't immediately fight a manual change.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime

from . import schedule as S
from .client import SpaUnreachable

_LOG = logging.getLogger("intex_spa.scheduler")

# desired-field -> the spa field / override key it maps to
_OVERRIDE_KEYS = {"power", "preset", "heater", "filter"}


class Scheduler:
    # how far ahead to average the forecast when sizing the climb rate
    WEATHER_LOOKAHEAD_H = 3.0

    def __init__(
        self,
        supervisor,
        config_path: str = "state/schedule.json",
        tick_seconds: float = 60.0,
        override_minutes: float = 60.0,
        weather=None,
    ) -> None:
        self.sup = supervisor
        self.config_path = config_path
        self.cfg = S.load_config(config_path) if config_path else dict(S.DEFAULT_CONFIG)
        self.tick_seconds = tick_seconds
        self.override_minutes = override_minutes
        self.weather = weather
        self._overrides: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self.last_plan: dict | None = None
        # latest effective °C/h, refreshed every tick (even when disabled) so the
        # UI can always show an ETA toward the setpoint. None until the first tick.
        self.heat_rate: float | None = None

    # -- config ---------------------------------------------------------------
    def get_config(self) -> dict:
        return self.cfg

    def set_config(self, cfg: dict) -> dict:
        if self.config_path:
            self.cfg = S.save_config(self.config_path, cfg)
        else:
            self.cfg = S.validate_config(cfg)
        return self.cfg

    # -- manual override ------------------------------------------------------
    def note_manual(self, *fields: str) -> None:
        until = _time.time() + self.override_minutes * 60
        for f in fields:
            self._overrides[f] = until

    def _overridden(self, field: str) -> bool:
        return self._overrides.get(field, 0.0) > _time.time()

    # -- heat rate ------------------------------------------------------------
    def current_heat_rate(self) -> float:
        """Latest learned °C/h, or the configured base until the first tick lands."""
        return self.heat_rate or float(self.cfg.get("heat_rate_c_per_h", 1.0))

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick_once()
            except Exception:  # noqa: BLE001 — never let the loop die
                _LOG.exception("scheduler tick failed")
            await asyncio.sleep(self.tick_seconds)

    # -- one evaluation + reconciliation -------------------------------------
    async def tick_once(self, now: datetime | None = None) -> S.Desired:
        now = now or datetime.now()
        cfg = self.cfg

        status = (self.sup.state or {}).get("status") or {}
        current = status.get("current_temp")
        points = self.sup.history.recent(hours=72)

        # refresh + read the forecast (best-effort) and size the climb rate from it.
        # Done every tick, before the enabled-gate, so `heat_rate`/`eta` stay fresh
        # for the UI even when automation is off (no spa writes happen here).
        air = None
        if self.weather is not None:
            now_epoch = now.timestamp()
            try:
                await self.weather.refresh(now=now_epoch)
                air = self.weather.air_window(
                    now_epoch, now_epoch + self.WEATHER_LOOKAHEAD_H * 3600
                )
            except Exception:  # noqa: BLE001 — weather is best-effort
                _LOG.warning("weather refresh failed during tick", exc_info=True)

        rate, rate_explain = S.effective_heat_rate(
            points, air, water=current, default=float(cfg.get("heat_rate_c_per_h", 1.0))
        )
        self.heat_rate = rate
        # ETA toward the spa's live setpoint — shown whether or not automation runs
        eta = S.eta_to_setpoint(now, current, status.get("preset_temp"), rate)

        if not cfg.get("enabled"):
            self.last_plan = {"enabled": False, "heat_rate": rate,
                              "rate_explain": rate_explain, "eta": eta,
                              "reasons": [{"kind": "scheduler_disabled"}],
                              "at": now.isoformat()}
            return S.Desired(reasons=[{"kind": "scheduler_disabled"}])

        desired = S.evaluate(cfg, now, current, rate)
        self.last_plan = {
            "enabled": True,
            "setpoint": desired.setpoint,
            "heater": desired.heater,
            "filter": desired.filter,
            "heat_rate": rate,
            "rate_explain": rate_explain,
            "preheat": S.next_preheat(cfg, now, current, rate),
            "eta": eta,
            "weather": self.weather.snapshot(now.timestamp()) if self.weather is not None else None,
            "reasons": desired.reasons,
            "at": now.isoformat(),
        }
        await self._reconcile(desired)
        return desired

    async def _reconcile(self, desired: S.Desired) -> None:
        if getattr(self.sup, "paused", False):
            # Pause halts ALL automated traffic to the controller. Keep
            # last_plan up to date (already done by the caller) so the UI
            # still shows what *would* happen — but don't write anything.
            return

        def st() -> dict:
            return (self.sup.state or {}).get("status") or {}

        try:
            if desired.power and not self._overridden("power") and not st().get("power"):
                await self.sup.set_field("power", True)
            if (
                desired.setpoint is not None
                and not self._overridden("preset")
                and st().get("preset_temp") != desired.setpoint
            ):
                await self.sup.set_preset(desired.setpoint)
            if (
                desired.heater is not None
                and not self._overridden("heater")
                and bool(st().get("heater")) != desired.heater
            ):
                await self.sup.set_field("heater", desired.heater)
            if (
                desired.filter is not None
                and not self._overridden("filter")
                and bool(st().get("filter")) != desired.filter
            ):
                await self.sup.set_field("filter", desired.filter)
        except SpaUnreachable:
            _LOG.info("spa unreachable during reconcile; will retry next tick")
