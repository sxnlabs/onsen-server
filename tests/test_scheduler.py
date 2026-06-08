"""Scheduler tests: tick_once drives the fake spa per the config."""

import json
import time
from datetime import datetime

from fake_spa import FakeSpa
from intex_spa.scheduler import Scheduler
from intex_spa.supervisor import Supervisor

MON = datetime(2026, 5, 18)  # Monday


async def _setup(tmp_path, cfg=None):
    spa = FakeSpa()
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    if cfg is not None:
        cfgpath.write_text(json.dumps(cfg))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999)
    return spa, sup, sch


async def _teardown(spa, sup):
    await sup.client.close()
    await spa.stop()


class _FakeWeather:
    """Minimal stand-in for WeatherClient: always reports a fixed cold air temp."""

    def __init__(self, air, feels=None):
        self.air = air
        self.feels = feels
        self.refreshed = False

    async def refresh(self, *, now=None, force=False):
        self.refreshed = True
        return True

    def air_window(self, start, end, key="air"):
        return self.feels if key == "feels" and self.feels is not None else self.air

    def air_now(self, now=None):
        return self.air

    def snapshot(self, now=None):
        snap = {"source": "fake", "air": self.air, "low_12h": self.air, "hours": 24}
        if self.feels is not None:
            snap["feels"] = self.feels
        return snap


async def test_weather_feeds_rate_explain_into_plan(tmp_path):
    cfg = {"enabled": True, "ready_by": [
        {"days": [0, 1, 2, 3, 4, 5, 6], "time": "10:00", "temp": 38}]}
    spa, sup, sch = await _setup(tmp_path, cfg=cfg)
    sch.weather = _FakeWeather(air=2.0)  # cold morning, no calibration data yet
    try:
        await sch.tick_once(now=MON.replace(hour=6))
        plan = sch.last_plan
        assert plan["weather"]["source"] == "fake"
        ex = plan["rate_explain"]
        assert ex["source"] == "weather-derate"   # cold derate (no history yet)
        assert ex["effective"] < ex["base"]        # 2°C outside slows the climb
        assert sch.weather.refreshed is True
        assert plan["preheat"]["temp"] == 38       # pre-heat plan exposed for the UI
    finally:
        await _teardown(spa, sup)


async def test_disabled_makes_no_changes(tmp_path):
    spa, sup, sch = await _setup(tmp_path, cfg={"enabled": False})
    try:
        before = dict(spa.state)
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state == before
    finally:
        await _teardown(spa, sup)


async def test_heat_rule_drives_setpoint_heater_filter(tmp_path):
    cfg = {"enabled": True, "heat_rules": [
        {"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 39}]}
    spa, sup, sch = await _setup(tmp_path, cfg=cfg)
    try:
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state["preset_temp"] == 39
        assert spa.state["heater"] is True
        assert spa.state["filter"] is True
    finally:
        await _teardown(spa, sup)


async def test_at_setpoint_keeps_heater_authorized_without_toggle(tmp_path):
    cfg = {"enabled": True, "heat_rules": [
        {"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 35}]}
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 35, "preset_temp": 35,
                   "filter": True, "heater": True})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps(cfg))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999)
    try:
        await sch.tick_once(now=MON.replace(hour=8))
        assert sch.last_plan["heater"] is True
        assert sch.last_plan["filter"] is True
        assert spa.state["heater"] is True
        assert spa.state["filter"] is True
        assert "heater" not in spa.intents[1:]
        assert "filter" not in spa.intents[1:]
    finally:
        await _teardown(spa, sup)


async def test_at_setpoint_does_not_restart_heater_once_off(tmp_path):
    cfg = {"enabled": True, "heat_rules": [
        {"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 25}]}
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 25, "preset_temp": 25,
                   "filter": False, "heater": False})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps(cfg))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999)
    try:
        await sch.tick_once(now=MON.replace(hour=1))
        assert sch.last_plan["heater"] is False
        assert spa.state["heater"] is False
        assert spa.state["filter"] is False
        assert "heater" not in spa.intents[1:]
        assert "filter" not in spa.intents[1:]

        spa.state["current_temp"] = 24
        await sup.refresh()
        await sch.tick_once(now=MON.replace(hour=2))
        assert sch.last_plan["heater"] is True
        assert spa.state["heater"] is True
        assert spa.state["filter"] is True
    finally:
        await _teardown(spa, sup)


async def test_cold_weather_widens_heater_restart_band(tmp_path):
    cfg = {"enabled": True, "heat_rules": [
        {"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 25}]}
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 24, "preset_temp": 25,
                   "filter": False, "heater": False})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps(cfg))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999)
    sch.weather = _FakeWeather(air=10.0)
    try:
        await sch.tick_once(now=MON.replace(hour=1))
        assert sch.last_plan["hysteresis"]["heater_on_undershoot"] == 2
        assert sch.last_plan["hysteresis"]["resume_temp"] == 23
        assert sch.last_plan["heater"] is False
        assert spa.state["heater"] is False
        assert spa.state["filter"] is False
        assert "heater" not in spa.intents[1:]
        assert "filter" not in spa.intents[1:]

        spa.state["current_temp"] = 23
        await sup.refresh()
        await sch.tick_once(now=MON.replace(hour=2))
        assert sch.last_plan["heater"] is True
        assert spa.state["heater"] is True
        assert spa.state["filter"] is True
    finally:
        await _teardown(spa, sup)


async def test_manual_override_blocks_field(tmp_path):
    cfg = {"enabled": True, "heat_rules": [
        {"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 39}]}
    spa, sup, sch = await _setup(tmp_path, cfg=cfg)
    try:
        sch.note_manual("heater")
        assert "heater" in sch.manual_overrides_remaining()
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state["heater"] is False    # override respected
        assert spa.state["preset_temp"] == 39  # other fields still managed
    finally:
        await _teardown(spa, sup)


async def test_manual_override_remaining_prunes_expired(tmp_path):
    spa, sup, sch = await _setup(tmp_path, cfg={"enabled": True})
    try:
        now = time.time()
        sch._overrides["heater"] = now + 90
        sch._overrides["filter"] = now - 1
        assert sch.manual_overrides_remaining(now=now) == {"heater": 90}
        assert "filter" not in sch._overrides
    finally:
        await _teardown(spa, sup)


async def test_manual_override_survives_scheduler_restart(tmp_path):
    spa, sup, sch = await _setup(tmp_path, cfg={"enabled": True})
    override_path = tmp_path / "manual_overrides.json"
    sch.override_path = override_path
    try:
        sch.note_manual("heater", "filter")

        restarted = Scheduler(
            sup,
            config_path=str(tmp_path / "schedule.json"),
            tick_seconds=9999,
            override_path=override_path,
        )

        remaining = restarted.manual_overrides_remaining()
        assert set(remaining) == {"heater", "filter"}
        assert remaining["heater"] > 0
        assert remaining["filter"] > 0
    finally:
        await _teardown(spa, sup)


async def test_expired_manual_override_is_not_restored(tmp_path):
    spa, sup, _sch = await _setup(tmp_path, cfg={"enabled": True})
    override_path = tmp_path / "manual_overrides.json"
    override_path.write_text(json.dumps({"heater": time.time() - 1, "filter": time.time() + 60}))
    try:
        restarted = Scheduler(
            sup,
            config_path=str(tmp_path / "schedule.json"),
            tick_seconds=9999,
            override_path=override_path,
        )

        assert restarted.manual_overrides_remaining().keys() == {"filter"}
    finally:
        await _teardown(spa, sup)


async def test_automation_cooldown_survives_scheduler_restart(tmp_path):
    spa, sup, sch = await _setup(tmp_path, cfg={"enabled": True})
    cooldown_path = tmp_path / "automation_cooldowns.json"
    sch.cooldown_path = cooldown_path
    try:
        sch._note_auto_change("heater")

        restarted = Scheduler(
            sup,
            config_path=str(tmp_path / "schedule.json"),
            tick_seconds=9999,
            min_automation_toggle_seconds=600,
            cooldown_path=cooldown_path,
        )

        remaining = restarted.automation_cooldowns_remaining()
        assert "heater" in remaining
        assert remaining["heater"] > 0
    finally:
        await _teardown(spa, sup)


async def test_sensor_error_cuts_heat_even_with_manual_override(tmp_path):
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 181, "filter": True, "heater": True})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps({"enabled": True, "eco_temp": 38}))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999)
    sch.note_manual("heater")
    try:
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state["heater"] is False
        assert spa.intents == ["status", "status", "heater"]
    finally:
        await _teardown(spa, sup)


async def test_sensor_error_cuts_heat_even_when_scheduler_disabled(tmp_path):
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 181, "filter": True, "heater": True})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps({"enabled": False, "eco_temp": 38}))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999)
    try:
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state["heater"] is False
    finally:
        await _teardown(spa, sup)


async def test_sensor_error_cuts_heat_even_when_paused(tmp_path):
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 181, "filter": True, "heater": True})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    sup.set_paused(True)
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps({"enabled": True, "eco_temp": 38}))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999)
    try:
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state["heater"] is False
    finally:
        await _teardown(spa, sup)


async def test_heater_on_without_filter_is_cut_even_when_scheduler_disabled(tmp_path):
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 30, "filter": False, "heater": True})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps({"enabled": False}))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999)
    try:
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state["heater"] is False
        assert any(r["kind"] == "no_circulation" for r in sch.last_plan["reasons"])
    finally:
        await _teardown(spa, sup)


async def test_automation_cooldown_prevents_filter_chatter(tmp_path):
    cfg = {"enabled": True, "eco_temp": 25,
           "filter_windows": [{"days": [0], "start": "08:00", "end": "09:00"}]}
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 30, "preset_temp": 25})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps(cfg))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999)
    sch.min_automation_toggle_seconds = 600
    try:
        await sch.tick_once(now=MON.replace(hour=8, minute=30))
        assert spa.state["filter"] is True
        await sch.tick_once(now=MON.replace(hour=9, minute=1))
        assert spa.state["filter"] is True
        assert spa.intents.count("filter") == 1
    finally:
        await _teardown(spa, sup)


async def test_safety_heater_off_bypasses_automation_cooldown(tmp_path):
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 181, "filter": True, "heater": True})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps({"enabled": True, "eco_temp": 38}))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999,
                    min_automation_toggle_seconds=600)
    sch._auto_changed_at["heater"] = time.time()
    try:
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state["heater"] is False
    finally:
        await _teardown(spa, sup)


async def test_filter_off_waits_for_heater_cooldown(tmp_path):
    cfg = {"enabled": True, "eco_temp": 25,
           "filter_windows": [{"days": [0], "start": "08:00", "end": "09:00"}]}
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 30, "preset_temp": 25,
                   "filter": True, "heater": True})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps(cfg))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999,
                    min_automation_toggle_seconds=600)
    sch._auto_changed_at["heater"] = time.time()
    try:
        await sch.tick_once(now=MON.replace(hour=9, minute=1))
        assert spa.state["heater"] is True
        assert spa.state["filter"] is True
        assert "heater" not in spa.intents[1:]
        assert "filter" not in spa.intents[1:]
    finally:
        await _teardown(spa, sup)


async def test_physical_panel_change_gets_manual_override(tmp_path):
    cfg = {"enabled": True, "heat_rules": [
        {"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 39}]}
    spa, sup, sch = await _setup(tmp_path, cfg=cfg)
    try:
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state["heater"] is True

        # Simulate the user pressing the spa's own control panel after the
        # automation command has settled.
        sch._ignore_observed_until.clear()
        spa.state["heater"] = False
        await sup.refresh()
        await sch.tick_once(now=MON.replace(hour=8, minute=1))

        assert spa.state["heater"] is False
        assert sch._overridden("heater") is True
    finally:
        await _teardown(spa, sup)


async def test_auto_command_reversion_does_not_create_manual_override(tmp_path):
    cfg = {"enabled": True, "heat_rules": [
        {"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 39}]}
    spa, sup, sch = await _setup(tmp_path, cfg=cfg)
    sch.min_automation_toggle_seconds = 600
    try:
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state["heater"] is True

        # Model the real controller reporting the heater back off immediately
        # after an automated toggle. That should be retried after cooldown, not
        # mistaken for a one-hour physical-panel override.
        spa.state["heater"] = False
        await sup.refresh()
        await sch.tick_once(now=MON.replace(hour=8, minute=1))

        assert spa.state["heater"] is False
        assert "heater" not in sch.manual_overrides_remaining()
        assert sch._overridden("heater") is False
    finally:
        await _teardown(spa, sup)


async def test_ready_by_preheats(tmp_path):
    cfg = {"enabled": True, "eco_temp": 30,
           "ready_by": [{"days": [0, 1, 2, 3, 4, 5, 6], "time": "10:00", "temp": 38}]}
    spa, sup, sch = await _setup(tmp_path, cfg=cfg)  # FakeSpa current_temp = 19
    try:
        await sch.tick_once(now=MON.replace(hour=9))
        assert spa.state["preset_temp"] == 38
        assert spa.state["heater"] is True
    finally:
        await _teardown(spa, sup)


async def test_ready_by_keeps_tight_restart_band_in_cold_weather(tmp_path):
    cfg = {"enabled": True, "eco_temp": 25, "heat_rate_c_per_h": 1.0,
           "ready_by": [{"days": [0, 1, 2, 3, 4, 5, 6], "time": "10:00", "temp": 35}]}
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 34, "preset_temp": 25,
                   "filter": False, "heater": False})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps(cfg))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999)
    sch.weather = _FakeWeather(air=2.0)
    try:
        await sch.tick_once(now=MON.replace(hour=9))
        assert sch.last_plan["hysteresis"]["source"] == "preheat"
        assert sch.last_plan["hysteresis"]["heater_on_undershoot"] == 1
        assert sch.last_plan["heater"] is True
        assert spa.state["heater"] is True
    finally:
        await _teardown(spa, sup)


async def test_ready_by_stays_latched_after_preheat_starts(tmp_path):
    cfg = {"enabled": True, "eco_temp": 25, "heat_rate_c_per_h": 2.0,
           "ready_by": [{"days": [0, 1, 2, 3, 4, 5, 6], "time": "18:00", "temp": 36}]}
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 30, "preset_temp": 25})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    cfgpath = tmp_path / "schedule.json"
    cfgpath.write_text(json.dumps(cfg))
    sch = Scheduler(sup, config_path=str(cfgpath), tick_seconds=9999)
    try:
        await sch.tick_once(now=MON.replace(hour=15, minute=10))
        assert spa.state["preset_temp"] == 36

        # With a 2 °C/h estimate, 31→36 would normally move the start to 15:30
        # and drop back to eco. Once started, ready-by must be monotone.
        spa.state["current_temp"] = 31
        await sup.refresh()
        await sch.tick_once(now=MON.replace(hour=15, minute=11))

        assert spa.state["preset_temp"] == 36
        assert sch.last_plan["preheat"]["active"] is True
        assert sch.last_plan["preheat"]["latched"] is True
        assert any(r["kind"] == "preheat_latched" for r in sch.last_plan["reasons"])
    finally:
        await _teardown(spa, sup)


async def test_set_config_persists(tmp_path):
    spa, sup, sch = await _setup(tmp_path, cfg={"enabled": False})
    try:
        new = sch.set_config({"enabled": True, "eco_temp": 31})
        assert new["eco_temp"] == 31
        assert sch.get_config()["enabled"] is True
        on_disk = json.loads((tmp_path / "schedule.json").read_text())
        assert on_disk["eco_temp"] == 31
    finally:
        await _teardown(spa, sup)


async def test_disabled_tick_still_exposes_rate_and_eta(tmp_path):
    # The scheduler is off, but the UI still wants an ETA toward the live setpoint.
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 30, "preset_temp": 35,
                   "heater": True})
    host, port = await spa.start()
    sup = Supervisor(host, port=port, poll_interval=9999)
    await sup.refresh()
    sch = Scheduler(sup, config_path=str(tmp_path / "s.json"), tick_seconds=9999)
    sch.set_config({"enabled": False, "heat_rate_c_per_h": 1.0})
    try:
        await sch.tick_once(now=MON.replace(hour=8))
        assert sch.last_plan["enabled"] is False
        assert sch.current_heat_rate() > 0
        assert sch.last_plan["eta"]["minutes"] == 300   # 5 °C at 1 °C/h
    finally:
        await sup.client.close()
        await spa.stop()
