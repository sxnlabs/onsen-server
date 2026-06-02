"""HTTP-level tests for the FastAPI app, end-to-end against the fake spa."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from fake_spa import FakeSpa
from web.main import create_app


@asynccontextmanager
async def app_for(spa: FakeSpa, **kw):
    """Start the fake spa, build the app, populate initial state, yield a client."""
    host, port = await spa.start()
    # no background polling, no on-disk history/schedule, no auth, no network weather
    kw.setdefault("weather_enabled", False)
    app = create_app(
        host, port=port, poll_interval=9999, history_path=None, schedule_path=None, **kw
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
        # the 7-day window the UI defaults to is served by the same endpoint
        r2 = await client.get("/history?hours=168")
        assert r2.status_code == 200
        assert len(r2.json()["points"]) >= 1


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
        w._hours = [{"t": base + i * 3600, "air": 8.0 + i, "feels": 7.0 + i, "wind": 10.0}
                    for i in range(6)]
        w._fetched_at = base
        r = await client.get("/weather")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["source"] == "open-meteo"
        assert body["air"] == 8.0          # air_now at base = first hour
        assert body["hours"] == 6


# NOTE: the SSE endpoint (/events) is not tested at the HTTP layer because
# httpx's ASGITransport buffers the full response before returning and therefore
# deadlocks on an infinite event stream. The fan-out logic the route depends on
# (subscribe / publish / state transitions) is covered in test_supervisor.py.
