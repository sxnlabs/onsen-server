"""HTTP-level tests for the FastAPI app, end-to-end against the fake spa."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from fake_spa import FakeSpa
from web.main import create_app, _history_activity


@asynccontextmanager
async def app_for(spa: FakeSpa, **kw):
    """Start the fake spa, build the app, populate initial state, yield a client."""
    host, port = await spa.start()
    # no background polling, no on-disk history/schedule, no auth, no network weather
    kw.setdefault("weather_enabled", False)
    kw.setdefault("pause_path", None)
    kw.setdefault("automation_cooldown_path", None)
    app = create_app(
        host,
        port=port,
        poll_interval=9999,
        history_path=None,
        schedule_path=None,
        command_log_path=None,
        manual_override_path=None,
        **kw,
    )
    await app.state.supervisor.refresh()  # deterministic initial snapshot
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            client.app = app  # tests may need app.state
            yield client
    finally:
        await app.state.supervisor.client.close()
        await spa.stop()


async def test_index_renders_state():
    spa = FakeSpa()
    async with app_for(spa) as client:
        r = await client.get("/")
        assert r.status_code == 200
        assert "19" in r.text          # current temp
        assert "37" in r.text          # setpoint
        assert "sse-connect" in r.text  # live wiring present


async def test_panel_partial():
    spa = FakeSpa()
    async with app_for(spa) as client:
        # default lang is EN — the english toggle labels are translated via i18n
        r = await client.get("/panel")
        assert r.status_code == 200
        assert "Bubbles" in r.text and "Heat" in r.text
        assert "🫧" in r.text  # toggle emoji rendered from ui_toggles


async def test_panel_shows_manual_override_countdown():
    spa = FakeSpa()
    async with app_for(spa) as client:
        client.app.state.scheduler.note_manual("heater")
        r = await client.get("/panel")
        assert r.status_code == 200
        assert "Manual action detected" in r.text
        assert "Heat" in r.text
        assert "60 min" in r.text


async def test_panel_uses_text_badge_when_temperature_is_reached():
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 35, "preset_temp": 35})
    async with app_for(spa) as client:
        r = await client.get("/panel")
        assert r.status_code == 200
        assert "mood-ok" in r.text
        assert ">✅<" in r.text
        assert "♨️" not in r.text


async def test_toggle_flips_spa_state():
    spa = FakeSpa()  # bubbles starts False
    async with app_for(spa) as client:
        r = await client.post("/toggle/bubbles")
        assert r.status_code == 200
        assert spa.state["bubbles"] is True
        # toggling again returns it
        await client.post("/toggle/bubbles")
        assert spa.state["bubbles"] is False


async def test_toggle_unknown_field_404():
    spa = FakeSpa()
    async with app_for(spa) as client:
        r = await client.post("/toggle/nope")
        assert r.status_code == 404


async def test_toggle_hidden_protocol_field_404():
    spa = FakeSpa()
    async with app_for(spa) as client:
        r = await client.post("/toggle/sanitizer")
        assert r.status_code == 404
        assert spa.state["sanitizer"] is False


async def test_preset_sets_temperature():
    spa = FakeSpa()
    async with app_for(spa) as client:
        r = await client.post("/preset/40")
        assert r.status_code == 200
        assert spa.state["preset_temp"] == 40


async def test_preset_out_of_range_400():
    spa = FakeSpa()
    async with app_for(spa) as client:
        assert (await client.post("/preset/50")).status_code == 400
        assert (await client.post("/preset/10")).status_code == 400
        assert spa.state["preset_temp"] == 37  # unchanged


async def test_panel_renders_quickset_grid():
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "preset_temp": 34})
    async with app_for(spa) as client:
        r = await client.get("/panel")
        assert r.status_code == 200
        assert "temp-grid" in r.text
        # balanced 25–40 steps of 3° (not the full degree-by-degree range)
        assert 'hx-post="/preset/25"' in r.text
        assert 'hx-post="/preset/34"' in r.text
        assert 'hx-post="/preset/40"' in r.text
        assert 'hx-post="/preset/36"' not in r.text  # off-step values omitted
        # the active setpoint chip is flagged current AND disabled — tapping it
        # would be a no-op preset that still arms a 60-min manual override
        assert "is-current" in r.text
        assert 'aria-current="true" disabled' in r.text
        # command buttons drive the in-flight (until-ACK) spinner
        assert 'hx-indicator="#cmd-spinner"' in r.text


async def test_index_wires_command_spinner():
    spa = FakeSpa()
    async with app_for(spa) as client:
        r = await client.get("/")
        assert r.status_code == 200
        # the spinner overlay lives OUTSIDE #panel so SSE swaps never drop it
        assert 'id="cmd-spinner"' in r.text
        assert 'hx-indicator="#cmd-spinner"' in r.text


async def test_heater_toggle_rejected_without_valid_water_temperature():
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 181})
    async with app_for(spa) as client:
        r = await client.post("/toggle/heater")
        assert r.status_code == 400
        assert spa.state["heater"] is False
        assert spa.state["filter"] is False


async def test_healthz():
    spa = FakeSpa()
    async with app_for(spa) as client:
        r = await client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["online"] is True
        assert body["paused"] is False     # default, no one paused yet
        assert isinstance(body["error"], bool)  # coarse only — never the raw error string


async def test_pause_toggle_round_trip():
    spa = FakeSpa()
    async with app_for(spa) as client:
        # default is not paused
        assert (await client.get("/healthz")).json()["paused"] is False

        # POST with no body → toggle on
        r = await client.post("/api/pause")
        assert r.status_code == 200 and r.json()["paused"] is True
        assert (await client.get("/healthz")).json()["paused"] is True

        # explicit off
        r2 = await client.post("/api/pause?state=off")
        assert r2.json()["paused"] is False

        # explicit on
        r3 = await client.post("/api/pause?state=on")
        assert r3.json()["paused"] is True

        # invalid → 400
        assert (await client.post("/api/pause?state=maybe")).status_code == 400


async def test_pause_persists_across_app_instances(tmp_path):
    pause_path = tmp_path / "pause.json"
    spa = FakeSpa()
    async with app_for(spa, pause_path=str(pause_path)) as client:
        r = await client.post("/api/pause?state=on")
        assert r.status_code == 200 and r.json()["paused"] is True

    spa2 = FakeSpa()
    async with app_for(spa2, pause_path=str(pause_path)) as client2:
        assert (await client2.get("/healthz")).json()["paused"] is True


async def test_pause_keeps_supervisor_refresh_active():
    spa = FakeSpa()
    async with app_for(spa) as client:
        sup = client.app.state.supervisor

        # Mutate the fake spa while paused. Pause blocks comfort automation, not
        # status polling: refresh remains the safety feed and keepalive.
        sup.set_paused(True)
        spa.state["preset_temp"] = 40
        await sup.refresh()
        assert sup.state["status"]["preset_temp"] == 40

        # Resume keeps reading normally.
        sup.set_paused(False)
        await sup.refresh()
        assert sup.state["status"]["preset_temp"] == 40


async def test_resume_preview_reports_pending_commands():
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 35, "preset_temp": 35,
                   "filter": True, "heater": True})
    async with app_for(spa) as client:
        await client.post("/api/pause?state=on")
        cfg = {"enabled": True, "eco_temp": 25,
               "heat_rules": [{"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 25}]}
        assert (await client.post("/api/schedule", json=cfg)).status_code == 200

        r = await client.get("/api/resume-preview")
        assert r.status_code == 200
        body = r.json()
        assert body["can_resume"] is True
        assert {"kind": "preset", "temp": 25} in body["actions"]
        assert {"kind": "toggle", "field": "heater", "desired": False} in body["actions"]


async def test_resume_blocks_unconfirmed_pending_commands():
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 35, "preset_temp": 35,
                   "filter": True, "heater": True})
    async with app_for(spa) as client:
        await client.post("/api/pause?state=on")
        cfg = {"enabled": True, "eco_temp": 25,
               "heat_rules": [{"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 25}]}
        await client.post("/api/schedule", json=cfg)

        r = await client.post("/api/pause?state=off")
        assert r.status_code == 409
        assert r.json()["detail"]["requires_confirm"] is True
        assert (await client.get("/healthz")).json()["paused"] is True
        assert spa.state["preset_temp"] == 35
        assert spa.state["heater"] is True


async def test_resume_allows_confirmed_pending_commands_without_sending_them_immediately():
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 35, "preset_temp": 35,
                   "filter": True, "heater": True})
    async with app_for(spa) as client:
        await client.post("/api/pause?state=on")
        cfg = {"enabled": True, "eco_temp": 25,
               "heat_rules": [{"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 25}]}
        await client.post("/api/schedule", json=cfg)

        r = await client.post("/api/pause?state=off&confirm=true")
        assert r.status_code == 200
        assert r.json()["paused"] is False
        assert spa.state["preset_temp"] == 35
        assert spa.state["heater"] is True


async def test_resume_allows_noop_plan_without_confirmation():
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 35, "preset_temp": 35,
                   "filter": True, "heater": True})
    async with app_for(spa) as client:
        await client.post("/api/pause?state=on")
        cfg = {"enabled": True,
               "heat_rules": [{"days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00", "temp": 35}]}
        await client.post("/api/schedule", json=cfg)

        r = await client.post("/api/pause?state=off")
        assert r.status_code == 200
        assert r.json()["paused"] is False


async def test_resume_blocks_when_spa_status_is_not_fresh():
    spa = FakeSpa()
    async with app_for(spa) as client:
        await client.post("/api/pause?state=on")
        await spa.stop()

        r = await client.post("/api/pause?state=off")
        assert r.status_code == 409
        assert "fresh online spa status" in r.json()["detail"]["message"]
        assert (await client.get("/healthz")).json()["paused"] is True


async def test_resume_blocks_when_live_status_read_times_out():
    spa = FakeSpa()
    async with app_for(spa) as client:
        await client.post("/api/pause?state=on")
        scheduler = client.app.state.scheduler
        scheduler.max_status_age_seconds = 0.01

        async def slow_refresh():
            await asyncio.sleep(0.1)

        client.app.state.supervisor.refresh = slow_refresh

        r = await client.post("/api/pause?state=off")
        assert r.status_code == 409
        assert r.json()["detail"]["preview"]["reason"] == "live_status_timeout"
        assert (await client.get("/healthz")).json()["paused"] is True


async def test_index_includes_scheduler_ui():
    spa = FakeSpa()
    async with app_for(spa) as client:
        r = await client.get("/")
        assert r.status_code == 200
        assert "Schedule" in r.text                # scheduler card on the main page (EN default)
        assert "/static/schedule.js" in r.text


async def test_schedule_api_get_and_save():
    spa = FakeSpa()
    async with app_for(spa) as client:
        r = await client.get("/api/schedule")
        assert r.status_code == 200
        assert r.json()["config"]["enabled"] is False  # default

        cfg = {"enabled": True, "eco_temp": 31,
               "heat_rules": [{"days": [0], "time": "07:00", "temp": 38}]}
        r2 = await client.post("/api/schedule", json=cfg)
        assert r2.status_code == 200 and r2.json()["ok"] is True

        r3 = await client.get("/api/schedule")
        assert r3.json()["config"]["enabled"] is True
        assert r3.json()["config"]["eco_temp"] == 31


async def test_schedule_api_rejects_bad_config():
    spa = FakeSpa()
    async with app_for(spa) as client:
        r = await client.post("/api/schedule", json={"heat_rules": [
            {"days": [9], "time": "07:00", "temp": 38}]})  # day 9 invalid
        assert r.status_code == 400


async def test_history_endpoint():
    spa = FakeSpa()
    async with app_for(spa) as client:
        r = await client.get("/history?hours=24")
        assert r.status_code == 200
        body = r.json()
        assert body["unit"] == "C"
        # the initial refresh in app_for recorded one sample
        assert len(body["points"]) >= 1
        assert body["points"][-1]["cur"] == 19
        assert body["points"][-1]["filter"] is False
        assert body["activity"] == {"heater": [], "filter": []}
        # the 7-day window the UI defaults to is served by the same endpoint
        r2 = await client.get("/history?hours=168")
        assert r2.status_code == 200
        assert len(r2.json()["points"]) >= 1


def test_history_activity_reconstructs_command_intervals(tmp_path):
    log = tmp_path / "commands.jsonl"
    rows = [
        {"t": 1100, "kind": "toggle", "field": "filter", "after": {"filter": True}},
        {"t": 1400, "kind": "toggle", "field": "filter", "after": {"filter": False}},
    ]
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    points = [
        {"t": 1000.0, "cur": 32, "set": 25, "heat": False},
        {"t": 1200.0, "cur": 32, "set": 25, "heat": True},
        {"t": 1300.0, "cur": 33, "set": 25, "heat": True},
        {"t": 1500.0, "cur": 33, "set": 25, "heat": False},
    ]

    activity = _history_activity(points, log)

    assert activity["heater"] == [{"start": 1200.0, "end": 1500.0}]
    assert activity["filter"] == [{"start": 1100.0, "end": 1400.0}]


def test_history_activity_point_state_can_close_command_interval(tmp_path):
    log = tmp_path / "commands.jsonl"
    log.write_text(json.dumps({
        "t": 1100,
        "kind": "toggle",
        "field": "filter",
        "after": {"filter": True},
    }) + "\n")
    points = [
        {"t": 1000.0, "cur": 32, "set": 25, "heat": False},
        {"t": 1200.0, "cur": 32, "set": 25, "heat": False},
        {"t": 1500.0, "cur": 33, "set": 25, "heat": False, "filter": False},
        {"t": 1600.0, "cur": 33, "set": 25, "heat": False, "filter": False},
    ]

    activity = _history_activity(points, log)

    assert activity["filter"] == [{"start": 1100.0, "end": 1500.0}]


async def test_history_endpoint_can_decimate_for_chart():
    spa = FakeSpa()
    async with app_for(spa) as client:
        hist = client.app.state.supervisor.history
        hist._pts = [
            {"t": float(i), "cur": i % 40, "set": 35, "heat": False}
            for i in range(1000)
        ]
        full = await client.get("/history?hours=999999")
        limited = await client.get("/history?hours=999999&max_points=100")
        assert full.status_code == 200 and limited.status_code == 200
        assert len(full.json()["points"]) == 1000
        pts = limited.json()["points"]
        assert len(pts) <= 100
        assert pts[0]["t"] == 0.0
        assert pts[-1]["t"] == 999.0

        tiny = await client.get("/history?hours=999999&max_points=3")
        assert [p["t"] for p in tiny.json()["points"]] == [0.0, 500.0, 999.0]


async def test_weather_disabled_endpoint():
    spa = FakeSpa()
    async with app_for(spa) as client:  # weather off by default in tests
        r = await client.get("/weather")
        assert r.status_code == 200
        assert r.json() == {"enabled": False}


async def test_weather_endpoint_reports_snapshot():
    spa = FakeSpa()
    async with app_for(spa, weather_enabled=True, weather_cache_path=None) as client:
        # lifespan isn't entered under ASGITransport, so populate the forecast directly
        w = client.app.state.weather
        import time
        base = time.time()
        w._hours = [{"t": base + i * 3600, "air": 8.0 + i, "feels": 7.0 + i,
                     "wind": 10.0, "code": 3}
                    for i in range(6)]
        w._sun = [{"sunrise": base - 2 * 3600, "sunset": base + 6 * 3600}]
        w._fetched_at = base
        r = await client.get("/weather")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["source"] == "open-meteo"
        assert body["air"] == 8.0          # air_now at base = first hour
        assert body["condition"] == "cloudy"
        assert body["sunrise"] == w._sun[0]["sunrise"]
        assert body["hours"] == 6


# NOTE: the SSE endpoint (/events) is not tested at the HTTP layer because
# httpx's ASGITransport buffers the full response before returning and therefore
# deadlocks on an infinite event stream. The fan-out logic the route depends on
# (subscribe / publish / state transitions) is covered in test_supervisor.py.
