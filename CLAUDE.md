# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This repo is the Onsen **server**. The project is split across three locations: the server (here, `~/Workspace/Web/onsen`), the native iOS/watchOS app (`~/Workspace/Apps/Onsen`), and the specs (`~/Specs/Onsen`). The server is the single-process FastAPI runtime that replaces the Intex iOS app for an Intex PureSpa (Baltik) on the LAN. It talks raw TCP to the spa's wifi module on port 8990. No cloud, no vendor SDK, protocol reverse-engineered from `mathieu-mp/aio-intex-spa` and validated against the real device (<spa-ip>). `~/Specs/Onsen/server.md` is the canonical server product spec — read it before this file for context.

## Commands

```bash
# run from the repo root

# first local provision
uv sync --extra dev

# dev (auto-reload, single process)
INTEX_SPA_HOST=<spa-ip> uv run uvicorn web.main:make_app --factory --reload

# all tests (offline — no spa needed; uses tests/fake_spa.py)
uv run pytest -q

# one test
uv run pytest tests/test_schedule.py::test_ready_by_leads_in -q

# read-only protocol diagnostic against the real device (stdlib only)
python3 probe.py <spa-ip>
python3 probe.py --selftest

# send one real SMS to check the alert path is armed (no socket to the spa,
# so it's safe against a live instance). --dry-run resolves the config only.
uv run python sms_probe.py
docker compose exec onsen /app/.venv/bin/python sms_probe.py   # remote deployment

# install as a LaunchAgent (com.sxnlabs.spa)
INTEX_SPA_HOST=<spa-ip> ./install.sh
```

Server test config lives in `pyproject.toml`: `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed), `pythonpath = [".", "tests"]`. Run these commands from the repo root.

## Deployment

Two supported runtimes, same one-process contract:

- **Local (Mac) — LaunchAgent.** `INTEX_SPA_HOST=<spa-ip> ./install.sh` (see Commands).
- **Remote (Linux) — Docker + WireGuard → UniFi.** Run off a server you own so the Mac can be off; a WireGuard tunnel terminated by the UniFi gateway lets the container reach the spa's LAN IP. Full runbook in [`DEPLOY.md`](DEPLOY.md).

```bash
# on the remote host, from the repo root
cp .env.example .env                                 # INTEX_SPA_HOST, HERMES_PASSWORD, WEATHER_*
cp wireguard/wg0.conf.example wireguard/wg0.conf     # paste the UniFi peer config
docker compose run --rm onsen python3 probe.py "$INTEX_SPA_HOST"   # read-only: tunnel + spa, BEFORE the app holds the socket
docker compose up -d --build
```

The spa accepts only ONE TCP client, so the LaunchAgent and the container must never run at the same time — stop one before starting the other. The image CMD runs a single uvicorn in `--factory` mode; never add `--workers`, never scale the `onsen` service. `Dockerfile` is multi-stage (Python 3.12 + `ffmpeg` for the live camera; no Python image deps — cover detection was removed).

## Architecture — the invariants that drive every design choice

**One TCP connection, one supervisor, one process.** The spa firmware accepts only a single TCP client on :8990. The whole app funnels through one `IntexSpaClient` (internal `asyncio.Lock`) owned by one `Supervisor`. The LaunchAgent runs uvicorn with **no `--workers` flag**, not `--workers 1` — the latter switches uvicorn to a multiprocess manager that spawns a child worker and wedges under launchd. Never add workers, never instantiate a second `IntexSpaClient` or `Supervisor`.

**Polling is keepalive.** The firmware drops the connection if nothing talks to it. The 10s status poll in `Supervisor._poll_loop` doubles as the live UI feed (via SSE) and as the connection keeper.

**Toggles, not absolutes.** Every functional command (`power`/`heater`/`filter`/`bubbles`/`jets`/`sanitizer`) *inverts* current state on the wire. `IntexSpaClient.set()` reads status first and only sends when current ≠ desired — that's what makes it idempotent. `preset_temp` is the one absolute command. If you add a new command, check `protocol.TOGGLE_FIELDS` first.

**Pure decision engine, async reconciler.** `intex_spa/schedule.py::evaluate()` is a pure function — given config + clock + temp + rate it returns a `Desired` dataclass. `intex_spa/scheduler.py` is the async tick loop that calls it and reconciles via the supervisor's idempotent sets. Keep this split: all rule logic stays unit-testable without a clock or a spa (see `tests/test_schedule.py`).

**Stale-but-useful on error.** `Supervisor._set_state` keeps the last known status across failed polls; the UI shows an offline banner with the last reading. Don't clear state on errors.

**Stale-but-useful is not "fine".** The flip side of the rule above: on 2026-08-22 the spa dropped off the network at 20:34 mid-heat and nothing said so for 46 hours — the panel showed a plausible stale reading and `/healthz` answered 200 the whole time. Two consequences to preserve. `supervisor.last_ok_at` is the *only* honest clock for an outage (`state["updated_at"]` moves on failed polls too), and `/healthz` stays 200 for the container healthcheck while `/spa-healthz` is the one that goes 503. Alerting is Onsen's own job, not Argos's: Argos runs on the same host, so it cannot be what notices that host is gone.

**Manual override window.** UI actions call `scheduler.note_manual(field)` to set a 60-min per-field freeze so the scheduler doesn't immediately revert a hand toggle. Any new write path from the UI must do the same.

**Python is pinned to 3.12 via `.python-version`.** CPython 3.14 silently killed the service after ~30 s under launchd on this machine (uvicorn's native deps — `uvloop`/`httptools`/`pydantic-core` — produced a segfault with no Python traceback). Don't bump the interpreter without first running the LaunchAgent for ≥30 min and checking `~/Library/Logs/DiagnosticReports/` for fresh Python `.ips` files. See `~/Specs/Onsen/server.md` "Design constraints" for the full story.

### Layered structure

```
intex_spa/protocol.py    pure encode/decode (no I/O, no deps) — checksum is mod 0xFF, not 0x100
intex_spa/client.py      one async TCP socket + lock + retries
intex_spa/supervisor.py  owns the client; poll loop; SSE fanout; history record on each refresh
intex_spa/history.py     JSONL temp samples, throttled (new point on a temp *or relay* change, or ≥60s), 7-day retention
intex_spa/weather.py     Open-Meteo client, in-memory + state/weather.json cache (30 min TTL), fail-soft
intex_spa/alerts.py      pure evaluate() (unreachable / error code / stalled heat / water floor) + AlertMonitor loop
intex_spa/sms.py         OVH SMS sender (stdlib urllib, v1 signature), opt-in, never raises; alerting_env() resolves env over state/.sms
sms_probe.py             one-shot "is the alert path armed?" — transport only, never touches the spa
intex_spa/errors.py      describe(exc) — never-empty one-liner for exceptions whose str() is ""
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
- `pause.json` — persisted automation pause flag; prevents restart from resuming comfort writes unexpectedly.
- `automation_cooldowns.json` — persisted relay cooldown timestamps; prevents restart from clearing anti-chatter protection.
- `weather.json` — last good Open-Meteo snapshot (kept across restarts so cold starts aren't blind).
- `alerts.json` — open alert episodes and whether each was already texted; this is what stops a restart mid-outage from sending the same SMS again.
- `.secret` — HMAC key for login cookies (generated on first run when `HERMES_PASSWORD` is set).
- `.password` — optional password file written by `install.sh` (alternative to the env var).
- `.sms` — optional `key=value` alerting config (recipient + OVH credentials) written by `install.sh`, 0600. launchd carries no environment, so on the LaunchAgent path this is where alerting is configured; the real env always wins over it (an explicitly empty `ONSEN_SMS_TO` disarms a stale file; deleting `state/.sms` is the permanent off switch). Kept out of the plist on purpose.

## Things to avoid

- **Inlining HTML** outside `web/templates/`. The UI is HTMX-driven: routes return either the full `index.html` shell or the `_panel.html` partial. Chart.js is the only client-side JS (loaded from `static/vendor/`, no CDN).
- **CDN deps.** All JS/CSS is vendored under `web/static/vendor/`. If you bump a version, re-vendor — the app must work offline (it's on the LAN).
- **Touching the spa from outside the supervisor.** Never construct an `IntexSpaClient` directly in a route or test against the real device — use `tests/fake_spa.py` for end-to-end tests.
- **Background work that can block startup.** Weather warmup is fired with `asyncio.create_task` in `lifespan` precisely because the network can be slow/down. New startup work follows the same pattern.
- **Hand-rolling timestamps in the UI.** All temp samples and forecasts are epoch seconds; the templates do the formatting.
