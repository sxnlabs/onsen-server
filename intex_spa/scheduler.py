"""Async scheduler: turns the decision engine into actions on the spa.

Each tick it gathers inputs (last known status, today's Tempo color, the learned
heat rate), asks `schedule.evaluate()` for the desired state, and reconciles the spa
toward it via the supervisor (whose sets are idempotent read-before-write). User
actions in the UI register a short-lived per-field manual override so the scheduler
doesn't immediately fight a manual change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from datetime import datetime
from pathlib import Path

from . import schedule as S
from .client import SpaUnreachable

_LOG = logging.getLogger("intex_spa.scheduler")

# desired-field -> the spa field / override key it maps to
_OVERRIDE_KEYS = {"power", "preset", "heater", "filter"}
_OVERRIDE_ORDER = ("power", "preset", "heater", "filter")
_OBSERVED_FIELDS = ("power", "heater", "filter", "preset_temp")
_OVERRIDE_FOR_STATUS_FIELD = {"preset_temp": "preset"}
_FILTER_AUTOSTOP_SECONDS = 2 * 60 * 60
_FILTER_AUTOSTOP_TOLERANCE_SECONDS = 10 * 60
# The autostop-detection timestamp must outlive the short relay-cooldown window:
# keep it loadable across a restart for the whole autostop horizon (plus tolerance)
# so a restart between the auto filter-start and the ~2h firmware autostop still
# recognizes the stop instead of mistaking it for a manual action.
_FILTER_AUTOSTOP_PERSIST_SECONDS = _FILTER_AUTOSTOP_SECONDS + _FILTER_AUTOSTOP_TOLERANCE_SECONDS


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
        override_path: str | Path | None = None,
        cooldown_path: str | Path | None = None,
    ) -> None:
        self.sup = supervisor
        self.config_path = config_path
        self.cfg = S.load_config(config_path) if config_path else dict(S.DEFAULT_CONFIG)
        self.tick_seconds = tick_seconds
        self.override_minutes = override_minutes
        self.min_automation_toggle_seconds = min_automation_toggle_seconds
        self.max_status_age_seconds = max_status_age_seconds
        self.weather = weather
        self.override_path = Path(override_path) if override_path else None
        self.cooldown_path = Path(cooldown_path) if cooldown_path else None
        self._overrides: dict[str, float] = {}
        self._auto_changed_at: dict[str, float] = {}
        # Last time the scheduler auto-started the filter — kept separately from
        # the cooldown map because the firmware autostop it guards against fires
        # ~2h later, far beyond the 10-min cooldown horizon.
        self._auto_filter_started_at: float | None = None
        self._ignore_observed_until: dict[str, float] = {}
        self._ready_by_latch: dict | None = None
        self._last_seen_status: dict | None = None
        self._task: asyncio.Task | None = None
        self.last_plan: dict | None = None
        # latest effective °C/h, refreshed every tick (even when disabled) so the
        # UI can always show an ETA toward the setpoint. None until the first tick.
        self.heat_rate: float | None = None
        self._load_overrides()
        self._load_auto_cooldowns()

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
        changed = False
        for f in fields:
            if f in _OVERRIDE_KEYS:
                self._overrides[f] = until
                changed = True
        if changed:
            self._save_overrides()

    def _overridden(self, field: str) -> bool:
        return self._overrides.get(field, 0.0) > _time.time()

    def manual_overrides_remaining(self, now: float | None = None) -> dict[str, int]:
        """Active manual scheduler holds, as field -> remaining whole seconds."""
        now = _time.time() if now is None else now
        active: dict[str, int] = {}
        pruned = False
        for field in _OVERRIDE_ORDER:
            until = self._overrides.get(field)
            if until is None:
                continue
            remaining = int(max(0.0, until - now))
            if remaining <= 0:
                self._overrides.pop(field, None)
                pruned = True
                continue
            active[field] = remaining
        if pruned:
            self._save_overrides()
        return active

    def _load_overrides(self) -> None:
        if self.override_path is None or not self.override_path.exists():
            return
        try:
            raw = json.loads(self.override_path.read_text())
        except (OSError, json.JSONDecodeError):
            _LOG.warning("failed to read manual override state", exc_info=True)
            return
        if not isinstance(raw, dict):
            return

        now = _time.time()
        for field, until in raw.items():
            if field not in _OVERRIDE_KEYS:
                continue
            try:
                until_f = float(until)
            except (TypeError, ValueError):
                continue
            if until_f > now:
                self._overrides[field] = until_f

    def _save_overrides(self) -> None:
        if self.override_path is None:
            return
        try:
            self.override_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.override_path.with_suffix(self.override_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._overrides, sort_keys=True))
            tmp.replace(self.override_path)
        except OSError:
            _LOG.warning("failed to persist manual override state", exc_info=True)

    # -- automation cooldown --------------------------------------------------
    def automation_cooldowns_remaining(self, now: float | None = None) -> dict[str, int]:
        now = _time.time() if now is None else now
        active: dict[str, int] = {}
        pruned = False
        for field in ("power", "heater", "filter"):
            last = self._auto_changed_at.get(field)
            if last is None:
                continue
            remaining = int(self.min_automation_toggle_seconds - (now - last))
            if remaining <= 0:
                self._auto_changed_at.pop(field, None)
                pruned = True
                continue
            active[field] = remaining
        if pruned:
            self._save_auto_cooldowns()
        return active

    def _note_auto_change(self, field: str) -> None:
        now = _time.time()
        self._auto_changed_at[field] = now
        # Record the auto filter-start on its own longer-lived horizon so the ~2h
        # autostop is still recognized after a restart (the cooldown entry above
        # is pruned/loaded on a 10-min horizon — too short for this).
        if field == "filter":
            self._auto_filter_started_at = now
        self._save_auto_cooldowns()

    def _load_auto_cooldowns(self) -> None:
        if self.cooldown_path is None or not self.cooldown_path.exists():
            return
        try:
            raw = json.loads(self.cooldown_path.read_text())
        except (OSError, json.JSONDecodeError):
            _LOG.warning("failed to read automation cooldown state", exc_info=True)
            return
        if not isinstance(raw, dict):
            return
        now = _time.time()
        # New format: {"cooldowns": {field: ts}, "filter_started_at": ts}.
        # Legacy format: a flat {field: ts} map (its "filter" entry doubled as the
        # autostop source) — still loaded so existing state survives the upgrade.
        nested = "cooldowns" in raw
        cooldowns = raw.get("cooldowns") if nested else raw
        if not isinstance(cooldowns, dict):
            cooldowns = {}
        for field, last in cooldowns.items():
            if field not in {"power", "heater", "filter"}:
                continue
            try:
                last_f = float(last)
            except (TypeError, ValueError):
                continue
            if (now - last_f) < self.min_automation_toggle_seconds:
                self._auto_changed_at[field] = last_f
        filter_started = raw.get("filter_started_at")
        if filter_started is None and not nested:
            filter_started = raw.get("filter")  # upgrade from the flat format
        if filter_started is not None:
            try:
                fs = float(filter_started)
            except (TypeError, ValueError):
                fs = None
            if fs is not None and (now - fs) < _FILTER_AUTOSTOP_PERSIST_SECONDS:
                self._auto_filter_started_at = fs

    def _save_auto_cooldowns(self) -> None:
        if self.cooldown_path is None:
            return
        try:
            self.cooldown_path.parent.mkdir(parents=True, exist_ok=True)
            data: dict = {"cooldowns": self._auto_changed_at}
            if self._auto_filter_started_at is not None:
                data["filter_started_at"] = self._auto_filter_started_at
            tmp = self.cooldown_path.with_suffix(self.cooldown_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, sort_keys=True))
            tmp.replace(self.cooldown_path)
        except OSError:
            _LOG.warning("failed to persist automation cooldown state", exc_info=True)

    # -- heat rate ------------------------------------------------------------
    def current_heat_rate(self) -> float:
        """Latest learned °C/h, or the configured base until the first tick lands."""
        return self.heat_rate or float(self.cfg.get("heat_rate_c_per_h", 1.0))

    async def _weather_inputs(self, now: datetime) -> tuple[float | None, float | None]:
        if self.weather is None:
            return None, None
        now_epoch = now.timestamp()
        try:
            await self.weather.refresh(now=now_epoch)
            end_epoch = now_epoch + self.WEATHER_LOOKAHEAD_H * 3600
            return (
                self.weather.air_window(now_epoch, end_epoch),
                self.weather.air_window(now_epoch, end_epoch, key="feels"),
            )
        except Exception:  # noqa: BLE001 — weather is best-effort
            _LOG.warning("weather refresh failed during tick", exc_info=True)
            return None, None

    def _plan_hysteresis(self, hysteresis: dict, desired: S.Desired) -> dict:
        out = dict(hysteresis)
        if any(
            isinstance(r, dict) and r.get("kind") in {"preheat", "preheat_latched"}
            for r in desired.reasons
        ):
            out["source"] = "preheat"
            out["heater_on_undershoot"] = S.HEATER_ON_UNDERSHOOT_C
        out["resume_temp"] = (
            desired.setpoint - int(out["heater_on_undershoot"])
            if desired.setpoint is not None
            else None
        )
        return out

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
        air, feels = await self._weather_inputs(now)
        hysteresis = S.weather_heater_hysteresis(air, feels)

        rate, rate_explain = S.effective_heat_rate(
            points, air, water=current, default=float(cfg.get("heat_rate_c_per_h", 1.0))
        )
        self.heat_rate = rate
        # ETA toward the spa's live setpoint — shown whether or not automation runs
        eta = S.eta_to_setpoint(now, current, status.get("preset_temp"), rate)

        desired = S.evaluate(
            cfg,
            now,
            current,
            rate,
            current_heater=status.get("heater"),
            heater_on_undershoot_c=hysteresis["heater_on_undershoot"],
        )
        self._apply_ready_by_latch(now, current, status.get("heater"), desired)
        self._apply_observed_safety(status, desired)
        plan_hysteresis = self._plan_hysteresis(hysteresis, desired)
        preheat = S.next_preheat(cfg, now, current, rate)
        preheat = self._latched_preheat_plan(now, preheat)

        if not cfg.get("enabled") and not self._safety_heater_off(desired):
            self.last_plan = {"enabled": False, "heat_rate": rate,
                              "rate_explain": rate_explain, "eta": eta,
                              "hysteresis": plan_hysteresis,
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
            "hysteresis": plan_hysteresis,
            "preheat": preheat,
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
            self._note_auto_change(field)
            self._ignore_observed_change(field)

        try:
            if desired.power and not self._overridden("power") and not st().get("power"):
                await auto_set("power", True)
            if (
                desired.setpoint is not None
                and not self._overridden("preset")
                and st().get("preset_temp") != desired.setpoint
            ):
                await self.sup.set_preset(desired.setpoint)
                self._ignore_observed_change("preset")
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
        return field in self.automation_cooldowns_remaining()

    async def resume_preview(self, now: datetime | None = None) -> dict:
        """Dry-run the commands automation would attempt if pause were lifted."""
        now = now or datetime.now()
        # Resuming automation is a control boundary. Require a live read even if
        # the cached status is still within the normal scheduler freshness window.
        live_read_ok = True
        try:
            await asyncio.wait_for(
                self.sup.refresh(),
                timeout=min(self.max_status_age_seconds, 8.0),
            )
        except asyncio.TimeoutError:
            live_read_ok = False
            _LOG.warning("resume preview live status read timed out")
        state = self.sup.state or {}
        updated_at = state.get("updated_at")
        status = state.get("status") or {}
        age = (_time.time() - float(updated_at)) if updated_at is not None else None
        fresh = bool(
            live_read_ok
            and state.get("online")
            and status
            and age is not None
            and age <= self.max_status_age_seconds
        )
        if not fresh:
            return {
                "can_resume": False,
                "reason": "live_status_timeout" if not live_read_ok else "stale_status",
                "online": bool(state.get("online")),
                "status_age_s": round(age, 1) if age is not None else None,
                "actions": [],
                "blocked": [],
            }

        air, feels = await self._weather_inputs(now)
        hysteresis = S.weather_heater_hysteresis(air, feels)
        desired = S.evaluate(
            self.cfg,
            now,
            status.get("current_temp"),
            self.current_heat_rate(),
            current_heater=status.get("heater"),
            heater_on_undershoot_c=hysteresis["heater_on_undershoot"],
        )
        self._apply_observed_safety(status, desired)
        actions, blocked = self._planned_changes(status, desired)
        return {
            "can_resume": True,
            "reason": None,
            "online": True,
            "status_age_s": round(age, 1),
            "actions": actions,
            "blocked": blocked,
            "desired": {
                "power": desired.power,
                "setpoint": desired.setpoint,
                "heater": desired.heater,
                "filter": desired.filter,
                "hysteresis": self._plan_hysteresis(hysteresis, desired),
                "reasons": desired.reasons,
            },
        }

    def _planned_changes(self, status: dict, desired: S.Desired) -> tuple[list[dict], list[dict]]:
        actions: list[dict] = []
        blocked: list[dict] = []
        cooldowns = self.automation_cooldowns_remaining()
        safety_heater_off = self._safety_heater_off(desired)

        def add_toggle(field: str, value: bool, *, safety: bool = False) -> None:
            if not safety and field in cooldowns:
                blocked.append({
                    "kind": "toggle",
                    "field": field,
                    "desired": value,
                    "reason": "cooldown",
                    "remaining": cooldowns[field],
                })
                return
            actions.append({"kind": "toggle", "field": field, "desired": value})

        if desired.power and not self._overridden("power") and not status.get("power"):
            add_toggle("power", True)
        if (
            desired.setpoint is not None
            and not self._overridden("preset")
            and status.get("preset_temp") != desired.setpoint
        ):
            actions.append({"kind": "preset", "temp": desired.setpoint})
        if (
            desired.heater is not None
            and (safety_heater_off or not self._overridden("heater"))
            and bool(status.get("heater")) != desired.heater
        ):
            add_toggle("heater", desired.heater, safety=safety_heater_off)
        if (
            desired.filter is not None
            and not self._overridden("filter")
            and bool(status.get("filter")) != desired.filter
        ):
            if (
                desired.filter is False
                and status.get("heater")
                and "heater" in cooldowns
            ):
                blocked.append({
                    "kind": "toggle",
                    "field": "filter",
                    "desired": False,
                    "reason": "heater_cooldown",
                    "remaining": cooldowns["heater"],
                })
            else:
                add_toggle("filter", desired.filter)
        return actions, blocked

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
                key = _OVERRIDE_FOR_STATUS_FIELD.get(field, field)
                if self._ignore_observed_until.get(key, 0.0) > _time.time():
                    continue
                if self._looks_like_filter_autostop(field, status):
                    continue
                changed.append(key)
        if changed:
            self.note_manual(*changed)

    def _looks_like_filter_autostop(self, field: str, status: dict) -> bool:
        if field != "filter":
            return False
        if self._last_seen_status is None:
            return False
        if self._last_seen_status.get("filter") is not True or status.get("filter") is not False:
            return False
        last_auto = self._auto_filter_started_at
        if last_auto is None:
            return False

        elapsed = _time.time() - last_auto
        return abs(elapsed - _FILTER_AUTOSTOP_SECONDS) <= _FILTER_AUTOSTOP_TOLERANCE_SECONDS

    def _remember_status(self) -> None:
        status = (self.sup.state or {}).get("status")
        if status:
            self._last_seen_status = dict(status)

    def _ignore_observed_change(self, field: str) -> None:
        # A just-issued automation command can echo back on the next poll, or fail
        # to stick and revert immediately. Neither is a physical-panel override.
        self._ignore_observed_until[field] = _time.time() + max(
            self.tick_seconds * 2,
            self.max_status_age_seconds + self.tick_seconds,
        )

    def _apply_ready_by_latch(
        self,
        now: datetime,
        current_temp: int | None,
        current_heater: bool | None,
        desired: S.Desired,
    ) -> None:
        active = next(
            (
                r for r in desired.reasons
                if isinstance(r, dict) and r.get("kind") == "preheat"
            ),
            None,
        )
        if active is not None:
            self._ready_by_latch = {
                "date": now.date().isoformat(),
                "time": active["for_time"],
                "temp": int(active["temp"]),
            }

        latch = self._ready_by_latch
        if latch is None:
            return
        target_dt = datetime.combine(
            datetime.fromisoformat(latch["date"]).date(),
            S.parse_hhmm(latch["time"]),
        )
        if now > target_dt:
            self._ready_by_latch = None
            return
        if current_temp is None:
            return

        target = int(latch["temp"])
        if desired.setpoint is None or desired.setpoint < target:
            desired.setpoint = target
        desired.heater = S._heater_demand(current_temp, target, current_heater)
        desired.filter = True if desired.heater else desired.filter
        desired.power = True if desired.heater or desired.filter else desired.power
        if active is None:
            desired.reasons.append({
                "kind": "preheat_latched",
                "temp": target,
                "for_time": latch["time"],
            })

    def _latched_preheat_plan(self, now: datetime, preheat: dict | None) -> dict | None:
        latch = self._ready_by_latch
        if latch is None:
            return preheat
        target_dt = datetime.combine(
            datetime.fromisoformat(latch["date"]).date(),
            S.parse_hhmm(latch["time"]),
        )
        if now > target_dt:
            return preheat
        plan = dict(preheat or {"time": latch["time"], "temp": int(latch["temp"])})
        plan["active"] = True
        plan["latched"] = True
        return plan

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
