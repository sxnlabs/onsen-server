"""Scheduler tests: tick_once drives the fake spa per the config."""

import json
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

    def __init__(self, air):
        self.air = air
        self.refreshed = False

    async def refresh(self, *, now=None, force=False):
        self.refreshed = True
        return True

    def air_window(self, start, end, key="air"):
        return self.air

    def air_now(self, now=None):
        return self.air

    def snapshot(self, now=None):
        return {"source": "fake", "air": self.air, "low_12h": self.air, "hours": 24}


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


async def test_manual_override_blocks_field(tmp_path):
    cfg = {"enabled": True, "heat_rules": [
        {"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 39}]}
    spa, sup, sch = await _setup(tmp_path, cfg=cfg)
    try:
        sch.note_manual("heater")
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state["heater"] is False    # override respected
        assert spa.state["preset_temp"] == 39  # other fields still managed
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
    sch._auto_changed_at["heater"] = __import__("time").time()
    try:
        await sch.tick_once(now=MON.replace(hour=8))
        assert spa.state["heater"] is False
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
