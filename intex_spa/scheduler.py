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
_OBSERVED_FIELDS = ("power", "heater", "filter", "preset_temp")
_OVERRIDE_FOR_STATUS_FIELD = {"preset_temp": "preset"}


class Scheduler:
    # how far ahead to average the forecast when sizing the climb rate
    WEATHER_LOOKAHEAD_H = 3.0

    def __init__(
        self,
        supervisor,
        config_path: str = "state/schedule.json",
        tick_seconds: float = 60.0,
        override_minutes: float = 60.0,
        min_automation_toggle_seconds: float = 10 * 60.0,
        max_status_age_seconds: float = 30.0,
        weather=None,
    ) -> None:
        self.sup = supervisor
        self.config_path = config_path
        self.cfg = S.load_config(config_path) if config_path else dict(S.DEFAULT_CONFIG)
        self.tick_seconds = tick_seconds
        self.override_minutes = override_minutes
        self.min_automation_toggle_seconds = min_automation_toggle_seconds
        self.max_status_age_seconds = max_status_age_seconds
        self.weather = weather
        self._overrides: dict[str, float] = {}
        self._auto_changed_at: dict[str, float] = {}
        self._last_seen_status: dict | None = None
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
            if f in _OVERRIDE_KEYS:
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

        await self._ensure_fresh_status()
        status = (self.sup.state or {}).get("status") or {}
        self._note_external_manual_changes(status)
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

        desired = S.evaluate(cfg, now, current, rate)
        self._apply_observed_safety(status, desired)

        if not cfg.get("enabled") and not self._safety_heater_off(desired):
            self.last_plan = {"enabled": False, "heat_rate": rate,
                              "rate_explain": rate_explain, "eta": eta,
                              "reasons": [{"kind": "scheduler_disabled"}],
                              "at": now.isoformat()}
            self._remember_status()
            return S.Desired(reasons=[{"kind": "scheduler_disabled"}])

        self.last_plan = {
            "enabled": bool(cfg.get("enabled")),
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
        self._remember_status()
        return desired

    async def _reconcile(self, desired: S.Desired) -> None:
        safety_heater_off = self._safety_heater_off(desired)
        if not (self.sup.state or {}).get("online") and not safety_heater_off:
            return
        if getattr(self.sup, "paused", False) and not safety_heater_off:
            # Pause halts ALL automated traffic to the controller. Keep
            # last_plan up to date (already done by the caller) so the UI
            # still shows what *would* happen — but don't write anything.
            return

        def st() -> dict:
            return (self.sup.state or {}).get("status") or {}

        async def auto_set(field: str, desired_value: bool, *, safety: bool = False) -> None:
            if not safety and self._automation_cooldown(field):
                _LOG.info("skip automated %s=%s during relay cooldown", field, desired_value)
                return
            if (
                field == "filter"
                and desired_value is False
                and st().get("heater")
                and self._automation_cooldown("heater")
            ):
                _LOG.info("skip automated filter=False while heater is in relay cooldown")
                return
            await self.sup.set_field(field, desired_value)
            self._auto_changed_at[field] = _time.time()

        try:
            if desired.power and not self._overridden("power") and not st().get("power"):
                await auto_set("power", True)
            if (
                desired.setpoint is not None
                and not self._overridden("preset")
                and st().get("preset_temp") != desired.setpoint
            ):
                await self.sup.set_preset(desired.setpoint)
            if (
                desired.heater is not None
                and (safety_heater_off or not self._overridden("heater"))
                and bool(st().get("heater")) != desired.heater
            ):
                await auto_set("heater", desired.heater, safety=safety_heater_off)
            if (
                desired.filter is not None
                and not self._overridden("filter")
                and bool(st().get("filter")) != desired.filter
            ):
                await auto_set("filter", desired.filter)
        except SpaUnreachable:
            _LOG.info("spa unreachable during reconcile; will retry next tick")

    def _automation_cooldown(self, field: str) -> bool:
        last = self._auto_changed_at.get(field)
        if last is None:
            return False
        return (_time.time() - last) < self.min_automation_toggle_seconds

    async def _ensure_fresh_status(self) -> None:
        state = self.sup.state or {}
        updated_at = state.get("updated_at")
        if state.get("status") is None or updated_at is None:
            await self.sup.refresh()
            return
        if (_time.time() - float(updated_at)) > self.max_status_age_seconds:
            await self.sup.refresh()

    def _note_external_manual_changes(self, status: dict) -> None:
        if not status:
            return
        if self._last_seen_status is None:
            self._last_seen_status = dict(status)
            return
        changed: list[str] = []
        for field in _OBSERVED_FIELDS:
            if self._last_seen_status.get(field) != status.get(field):
                changed.append(_OVERRIDE_FOR_STATUS_FIELD.get(field, field))
        if changed:
            self.note_manual(*changed)

    def _remember_status(self) -> None:
        status = (self.sup.state or {}).get("status")
        if status:
            self._last_seen_status = dict(status)

    def _apply_observed_safety(self, status: dict, desired: S.Desired) -> None:
        if status.get("heater") and not status.get("filter"):
            desired.heater = False
            desired.reasons.append({"kind": "no_circulation", "field": "filter"})

    def _safety_heater_off(self, desired: S.Desired) -> bool:
        if desired.heater is not False:
            return False
        return any(
            isinstance(r, dict) and r.get("kind") in {"sensor_error", "no_circulation"}
            for r in desired.reasons
        )
