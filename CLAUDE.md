# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Single-process FastAPI web app that replaces the Intex iOS app for an Intex PureSpa (Baltik) on the LAN. Talks raw TCP to the spa's wifi module on port 8990. No cloud, no vendor SDK, protocol reverse-engineered from `mathieu-mp/aio-intex-spa` and validated against the real device (<spa-ip>). The README is the canonical product spec — read it before this file for context.

## Commands

```bash
# dev (auto-reload, single process)
INTEX_SPA_HOST=<spa-ip> uv run uvicorn web.main:make_app --factory --reload

# all tests (offline — no spa needed; uses tests/fake_spa.py)
uv run pytest -q

# one test
uv run pytest tests/test_schedule.py::test_ready_by_leads_in -q

# read-only protocol diagnostic against the real device (stdlib only)
python3 probe.py <spa-ip>
python3 probe.py --selftest

# install as a LaunchAgent (com.sxnlabs.spa)
INTEX_SPA_HOST=<spa-ip> ./install.sh
```

Test config lives in `pyproject.toml`: `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed), `pythonpath = [".", "tests"]`.

## Architecture — the invariants that drive every design choice

**One TCP connection, one supervisor, one process.** The spa firmware accepts only a single TCP client on :8990. The whole app funnels through one `IntexSpaClient` (internal `asyncio.Lock`) owned by one `Supervisor`. The LaunchAgent runs uvicorn with **no `--workers` flag**, not `--workers 1` — the latter switches uvicorn to a multiprocess manager that spawns a child worker and wedges under launchd. Never add workers, never instantiate a second `IntexSpaClient` or `Supervisor`.

**Polling is keepalive.** The firmware drops the connection if nothing talks to it. The 10s status poll in `Supervisor._poll_loop` doubles as the live UI feed (via SSE) and as the connection keeper.

**Toggles, not absolutes.** Every functional command (`power`/`heater`/`filter`/`bubbles`/`jets`/`sanitizer`) *inverts* current state on the wire. `IntexSpaClient.set()` reads status first and only sends when current ≠ desired — that's what makes it idempotent. `preset_temp` is the one absolute command. If you add a new command, check `protocol.TOGGLE_FIELDS` first.

**Pure decision engine, async reconciler.** `intex_spa/schedule.py::evaluate()` is a pure function — given config + clock + temp + rate it returns a `Desired` dataclass. `intex_spa/scheduler.py` is the async tick loop that calls it and reconciles via the supervisor's idempotent sets. Keep this split: all rule logic stays unit-testable without a clock or a spa (see `tests/test_schedule.py`).

**Stale-but-useful on error.** `Supervisor._set_state` keeps the last known status across failed polls; the UI shows an offline banner with the last reading. Don't clear state on errors.

**Manual override window.** UI actions call `scheduler.note_manual(field)` to set a 60-min per-field freeze so the scheduler doesn't immediately revert a hand toggle. Any new write path from the UI must do the same.

**Python is pinned to 3.12 via `.python-version`.** CPython 3.14 silently killed the service after ~30 s under launchd on this machine (uvicorn's native deps — `uvloop`/`httptools`/`pydantic-core` — produced a segfault with no Python traceback). Don't bump the interpreter without first running the LaunchAgent for ≥30 min and checking `~/Library/Logs/DiagnosticReports/` for fresh Python `.ips` files. See README "Design constraints" for the full story.

### Layered structure

```
intex_spa/protocol.py    pure encode/decode (no I/O, no deps) — checksum is mod 0xFF, not 0x100
intex_spa/client.py      one async TCP socket + lock + retries
intex_spa/supervisor.py  owns the client; poll loop; SSE fanout; history record on each refresh
intex_spa/history.py     JSONL temp samples, throttled (new point on change or ≥60s), 7-day retention
intex_spa/weather.py     Open-Meteo client, in-memory + state/weather.json cache (30 min TTL), fail-soft
intex_spa/schedule.py    config validation + pure evaluate() + effective_heat_rate() calibration
intex_spa/scheduler.py   async reconciler, manual-override tracking, weather-aware heat-rate sizing
web/main.py              FastAPI factory; lifespan starts supervisor + scheduler; HTMX/SSE routes
web/auth.py              optional signed-cookie gate (HERMES_PASSWORD); UI-only, not spa-port
```

### State files (under `state/`, all auto-managed)

- `history.jsonl` — temp samples (water/setpoint/heater/air); 7-day retention; self-healing on corrupt lines.
- `commands.jsonl` — append-only audit trail of app-initiated spa writes (toggle/preset, before/after/error).
- `schedule.json` — user-edited schedule, served by `GET/POST /api/schedule`.
- `manual_overrides.json` — active per-field manual scheduler holds, so UI/physical-panel overrides survive restarts.
- `weather.json` — last good Open-Meteo snapshot (kept across restarts so cold starts aren't blind).
- `.secret` — HMAC key for login cookies (generated on first run when `HERMES_PASSWORD` is set).
- `.password` — optional password file written by `install.sh` (alternative to the env var).

## Things to avoid

- **Inlining HTML** outside `web/templates/`. The UI is HTMX-driven: routes return either the full `index.html` shell or the `_panel.html` partial. Chart.js is the only client-side JS (loaded from `static/vendor/`, no CDN).
- **CDN deps.** All JS/CSS is vendored under `web/static/vendor/`. If you bump a version, re-vendor — the app must work offline (it's on the LAN).
- **Touching the spa from outside the supervisor.** Never construct an `IntexSpaClient` directly in a route or test against the real device — use `tests/fake_spa.py` for end-to-end tests.
- **Background work that can block startup.** Weather warmup is fired with `asyncio.create_task` in `lifespan` precisely because the network can be slow/down. New startup work follows the same pattern.
- **Hand-rolling timestamps in the UI.** All temp samples and forecasts are epoch seconds; the templates do the formatting.
