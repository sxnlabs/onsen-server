# spa — local web control for the Intex PureSpa (Baltik)

Replaces the Intex iOS app with a single-process web UI served from a Mac on
your LAN. Talks directly to the spa's wifi module over TCP/8990 — no cloud, no
vendor account, no auth. Protocol reverse-engineered from
[`mathieu-mp/aio-intex-spa`](https://github.com/mathieu-mp/aio-intex-spa) and
validated byte-for-byte against a real PureSpa Baltik.

**What you get on top of `aio-intex-spa`:** a mobile-first web UI, a weather-aware
heat scheduler (pre-heats earlier when it's cold outside), a built-in camera
subsystem (snapshot, daily timelapse, experimental spa-cover ON/OFF detection),
and a launchd service that's
debugged for the silent failure modes you'll hit otherwise. Built for one
specific Mac → spa setup; the code is licensed permissively so you can fork.

## Screenshots

### Desktop

![Desktop layout — temperature gauge, toggles, 7-day chart, camera, weather, scheduler](docs/screenshots/desktop-overview.png)

*Two-column desktop layout: spa controls + temperature history on the left,
weather + schedule on the right, with a live camera card below the chart.*

![Camera card with the settings panel open — desktop](docs/screenshots/desktop-camera-settings.png)

*Camera card detail with the ⚙ settings panel open: housse (spa cover) state,
three-way **Auto / En place / Retirée** manual override, ROI redraw, one-click
visual-baseline learning. Cover detection is intentionally experimental — only
a partial view of the spa is in frame.*

### Mobile

![Camera card with settings panel — mobile](docs/screenshots/mobile-camera-settings.png)

*Single-column stack on phones (primary use-case via ngrok / Tailscale).*

## Layout

```
spa/
├── intex_spa/          # protocol + runtime core
│   ├── protocol.py     # checksum / request framing / status decode (pure, dep-free)
│   ├── client.py       # single asyncio TCP connection + lock; toggle read-before-write
│   ├── supervisor.py   # owns the one client, polls (=keepalive), fans out state via SSE
│   ├── history.py      # throttled temperature history (JSONL, self-healing)
│   ├── schedule.py     # config + pure decision engine (heat / filter / ready-by)
│   ├── scheduler.py    # async reconciler: applies the desired state, manual overrides
│   ├── camera.py       # RTSP/RTSPS camera bridge: ffmpeg snapshot loop + timelapse
│   ├── cover_detect.py # experimental ROI luma/std-dev cover ON/OFF heuristic (optional deps)
├── web/
│   ├── main.py         # FastAPI app (factory) + routes + SSE
│   ├── templates/      # index.html + _panel.html (HTMX) + recap.html (timelapse + day stats)
│   └── static/         # app.css, schedule.js, camera.js (chart overlay + ROI calibrator)
├── tests/              # offline: protocol vectors, client/supervisor e2e (fake spa), HTTP
├── probe.py            # standalone first-contact diagnostic (stdlib only)
└── install.sh          # uv sync + LaunchAgent (com.sxnlabs.spa)
```

## Run

> **First time on this machine?** Read [`SETUP.md`](SETUP.md) — the end-to-end
> bootstrap from a fresh Mac (brew + uv + ffmpeg, venv, `state/camera.json`,
> LaunchAgent, ngrok tunnel, state restore from backup).

Dev:

```bash
cd ~/Hermes/apps/spa
uv sync
INTEX_SPA_HOST=<spa-ip> uv run uvicorn web.main:make_app --factory --reload
```

Tests (no hardware needed):

```bash
uv run pytest -q
```

Probe the spa directly (read-only, zero deps):

```bash
python3 probe.py <spa-ip>            # one shot
python3 probe.py <spa-ip> --watch 10 # poll
python3 probe.py --selftest                # offline decode check
```

## Install as a service

```bash
# localhost only:
INTEX_SPA_HOST=<spa-ip> ./install.sh
# reachable from the iPhone on the LAN:
HERMES_HOST=0.0.0.0 INTEX_SPA_HOST=<spa-ip> ./install.sh
```

Config (env vars): `INTEX_SPA_HOST` (required), `INTEX_SPA_PORT` (8990),
`INTEX_SPA_POLL` (10s), `HERMES_HOST` (127.0.0.1), `HERMES_PORT` (8731),
`HERMES_PASSWORD` (optional — gate the UI).

Uninstall: `launchctl bootout gui/$(id -u)/com.sxnlabs.spa && rm ~/Library/LaunchAgents/com.sxnlabs.spa.plist`

## Security — read this before exposing it

**The spa firmware has no authentication and no encryption.** Anyone who can open a
TCP socket to `<spa-ip>:8990` can control it directly, bypassing this app
entirely. The single-client quirk (only one TCP client at a time) is a race, not a
control. The real protection is **network-level**:

- Add a firewall / ACL rule on your router so **only the Mac's IP** can reach
  `<spa-ip>:8990`; drop everything else. Most routers expose this under
  "Traffic Rules", "Access Control", "Inter-VLAN rules", or similar — the
  spa being on its own IoT VLAN makes this even easier. See
  [`SETUP.md` § 2.5](SETUP.md) for the concrete pattern.
- **Block the spa's WAN egress** (stops phone-home and a possible Tuya firmware push).

The app's **optional password** (`HERMES_PASSWORD`, or `state/.password`) is
defense-in-depth for the **web UI only** — single shared password, signed-cookie
session (login once, persists). It does nothing for the open port above. The
installer warns if you bind `0.0.0.0` without a password.

## Design constraints (why it's built this way)

- **One TCP connection.** The firmware tolerates a single client on :8990, so the
  whole app funnels through one `IntexSpaClient` (internal lock) owned by one
  `Supervisor`. Run uvicorn as a **single in-process server** — the LaunchAgent runs it
  with **no `--workers` flag at all**. (Do not "pin" `--workers 1`: that flag switches
  uvicorn to a multiprocess manager that spawns a child worker, which wedges under
  launchd. The default single process is what gives us exactly one supervisor.)
- **Polling = keepalive.** The firmware drops the connection if nothing talks to it,
  so the 10s status poll keeps it warm and feeds the live UI.
- **Toggles, not absolutes.** Every functional command (power/heater/filter/bubbles)
  *inverts* current state. The client reads status first and only sends when the
  desired state differs — idempotent. `preset_temp` is the one absolute command.
- **Stale-but-useful on error.** On a dropped/unreachable spa the UI shows an offline
  banner while keeping the last known reading; the next poll recovers.
- **Python is pinned to 3.12** (`.python-version`). On this Mac, CPython **3.14**
  produced silent ~30 s deaths under launchd — uvicorn process exits with no traceback
  and no Python-side shutdown hook, the poll loop just stops. `uvicorn[standard]` pulls
  three native deps (`uvloop`, `httptools`, `pydantic-core`), and at least one of them
  segfaulted in the background long-running case on 3.14. Foreground runs survived
  because the user kept the process alive interactively. Don't bump without testing
  the LaunchAgent for ≥30 min and checking `~/Library/Logs/DiagnosticReports/` for
  fresh Python `.ips` reports. To bump: `uv python pin 3.X && rm -rf .venv && uv sync`.
- **LaunchAgent hardening.** The plist sets `ThrottleInterval=15` (cap respawn rate so
  a fast-failing process can't peg launchd into the "inefficient checked allocations"
  state), `ExitTimeOut=20` (lifespan shutdown needs to close the spa TCP socket and
  cancel the poll task — 5 s is too tight), `ProcessType=Adaptive` (NOT Background —
  Background drops the jetsam priority so the OS kills us first under memory pressure),
  and `HOME` is set explicitly in `EnvironmentVariables` because launchd does not
  inherit it for LaunchAgents (httpx/anyio trust stores need it).

## Known limits / gotchas

- **`heater` is the function toggle, not the element state.** The firmware reports
  whether heating is *enabled*, not whether it's drawing power. A Shelly EM on the
  spa's supply would close that gap (and give kWh telemetry).
- **macOS sleep** suspends the LaunchAgent. Enable *Settings → Energy → Wake for
  network access* (and keep the Mac on power) to stay reachable from the phone.
- **Inter-VLAN.** If the spa is on a separate VLAN/SSID from the Mac, your
  router must allow LAN-side traffic to `<spa-ip>:8990`. Either sit them on
  the same network, or add the firewall exception in your router admin UI.
- **Assets are vendored** under `static/vendor/` (htmx, the SSE extension, Chart.js) —
  no CDN, works fully offline. Re-vendor with `npm i` + copy if you bump versions.
- Temperature setpoint is bounded to 20–40 °C (`protocol.TEMP_MIN_C/MAX_C`).

## History & chart

The supervisor records a throttled temperature sample on each successful poll (a new
point only when the temp changes or ≥60 s elapsed), persisted to `state/history.jsonl`
(7-day retention) so it survives restarts. Each sample also carries the outside air temp
(`air`) when the weather client has data. `GET /history?hours=N` serves it; the UI draws
water temp + setpoint + outside air with Chart.js and a 24 h / 7 j range toggle (defaults
to 7 j; x-axis switches to day/month labels for the week view).

## Scheduler

A built-in scheduler replaces the Intex app's clunky timer (the device's native timer
isn't exposed by the LAN protocol, so we run our own — strictly more flexible). The
engine (`schedule.evaluate`) is pure and fully unit-tested; the async reconciler applies
the desired state each minute via the supervisor (idempotent sets). Edited inline on the
main page (a form UI — day chips, time pickers, temp inputs; no JSON), persisted to
`state/schedule.json`, served by `GET/POST /api/schedule`. Features:

- **Timed heat setpoint** (`heat_rules`) — thermostat schedule: target temp by time/day.
- **Filtration windows** (`filter_windows`) — run filtration on a timer; also forced on
  whenever heating (circulation required).
- **"Ready by" pre-heat** (`ready_by`) — reach a temp by a target time; the start time is
  estimated from the heat rate, which **starts at 1 °C/h and is learned from history**
  (`estimate_heat_rate`, falling back to `heat_rate_c_per_h`).

The heater follows demand (on while below or exactly at setpoint, off above; the spa's
thermostat holds). Manual UI actions and physical-panel changes register a per-field override
(default 60 min) in `state/manual_overrides.json` so the scheduler won't immediately
revert a manual change, even across a service restart. The card shows the live plan
(setpoint, heat/rest, filter, learned rate).

## Weather-aware pre-heat

The spa loses heat (and so climbs slower) roughly in proportion to (water − outside air),
so the "ready by" lead must grow when it's cold. `intex_spa/weather.py` is a cached,
dependency-free Open-Meteo client (location set via `WEATHER_LAT` / `WEATHER_LON` env
vars; defaults to Brest, France — change them for yours): it fetches the hourly
forecast (real air, apparent/feels-like, wind) in a worker thread, caches it in memory +
`state/weather.json` (30 min TTL), and degrades gracefully (keeps the last good forecast
on any error). Read helpers (`air_now`, `air_window`, `low_ahead`, `snapshot`) are pure
and instant — the supervisor stamps `air` into history via a non-blocking `air_provider`,
the scheduler refreshes the forecast each tick.

The heat-rate fed to the pre-heat math is computed by `schedule.effective_heat_rate`:

- **Calibrated** (preferred): once enough air-stamped history exists, `calibrate_rates`
  learns `r_net = r_gross − k_loss·(water − air)` from the spa's own heating (rising) and
  cooling (falling) segments — no hardcoded wattage. `predict_heat_rate(water, air, …)`
  then gives the achievable °C/h at the forecast air temp.
- **Weather-derate** (bootstrap, before enough data): the measured base rate is scaled by
  `1 − 0.025·max(0, 15 − air)`, floored at ×0.5 — a gentle, legible cold penalty.
- **Measured** (no weather): the plain `estimate_heat_rate` scalar, as before.

A colder forecast lowers the effective rate → `evaluate`'s `lead = gap / rate` grows → the
window opens earlier. `GET /weather` returns the snapshot plus the live `rate_explain`
and `preheat` (target, computed start, lead hours); the UI's **Météo extérieure** card
shows current conditions and spells out the algorithm's decision so it's auditable.

Config (env, defaults shown): `WEATHER_ENABLED=1`, `WEATHER_LAT=48.45`, `WEATHER_LON=-4.42`.

## Camera

The camera partially shows the spa. The subsystem wires four features into a
single fail-soft module (`intex_spa/camera.py`) that mirrors `weather.py` —
same lifecycle, same stale-but-useful pattern, same "no config ⇒ off" master
switch.

> **Vendor-agnostic.** Any camera that speaks RTSP / RTSPS works — `ffmpeg`
> reads the URL, it doesn't care who made the camera. The examples below use
> a UniFi-style RTSPS URL because that's the author's setup; substitute your
> camera's RTSP URL accordingly.

**Master switch.** Everything is gated by `state/camera.json`. Missing file or empty
`rtsps_url` ⇒ no background tasks, every endpoint returns `{"enabled": false}`, the
UI card is not rendered. The full shape (gitignored — never committed):

```json
{
  "rtsps_url": "rtsps://<udm-ip>:7441/<TOKEN>?enableSrtp",
  "poll_seconds": 10,
  "snapshot_max_width": 1280,
  "jpeg_quality": 7,
  "timelapse_retention_days": 7,
  "roi": null
}
```

System deps: `brew install ffmpeg` (system binary; verified with 8.x). Python deps
for cover detection are optional — install with `uv sync --extra camera` (pulls
`Pillow`, `numpy`). Core + offline test suite need none of them.

### Feature 1 — live snapshot

`CameraSnapshot` polls a single frame every `poll_seconds` (default 10 s) via
`ffmpeg -rtsp_transport tcp -f image2 -vf scale=…` running in a worker thread, and
writes `state/cam.jpg` atomically (`tmp + replace`). The frame is downscaled to
`snapshot_max_width` (default 1280 → ~200 KB/frame at q:v 7) so the file stays small
on disk and over the LAN. `GET /camera.jpg` serves the last frame with
`Cache-Control: no-store`; the UI tile under the temperature chart auto-refreshes.
On any ffmpeg failure the previous frame stays — same stale-but-useful pattern as
the spa supervisor.

> **Gotcha (the silent-fail one).** Without `-f image2`, ffmpeg picks the output
> muxer from the file extension. Our atomic-write tmp file is `cam.jpg.tmp` →
> "Unable to choose an output format" → silent timeout from the launchd process
> (no Python traceback). Forcing the muxer fixes it. Test pinned.

### Feature 2 — cover ON / OFF (experimental)

`intex_spa/cover_detect.py` crops a user-calibrated ROI from the latest frame and
classifies it with a simple luma + std-dev heuristic: a uniformly dark patch reads
"cover ON", a brighter / specular / varied patch reads "cover OFF", everything in
between stays "unknown". Pillow + numpy are gated — without them every call
returns `unknown`.

Calibrate from the UI: **Calibrer** button → drag a rectangle on the live tile
covering the visible water surface → **Enregistrer**. The ROI persists into
`state/camera.json` and is mapped back to native frame pixels so it survives a
resolution change. Latest classification is shown as a pill badge on the camera
tile and stored in `state/cover_state.json`.

The detection is intentionally **not wired into the scheduler** in v1 — the
partial view makes it unreliable. The plumbing is there (see `cover_detect.LUMA_*`
thresholds) if a week of validated data later justifies feeding it into
`effective_heat_rate` / `k_loss`.

### Feature 3 — timelapse + daily recap

Each successful snapshot is hard-linked once per minute (`timelapse_every_seconds`)
into `state/cam_history/YYYY-MM-DD/HH-MM-SS.jpg`. The dated directories are pruned
to `timelapse_retention_days` (default 7) by an opportunistic hourly sweep inside
the same poll loop. `GET /timelapse?date=YYYY-MM-DD` calls ffmpeg on demand to
concat the day's frames into an mp4 (`libx264 -crf 23`), caches the result at
`state/cam_history/<date>.mp4`, and serves it; a new frame on the same day
invalidates the cached mp4 (deleted on archive) so a re-request always picks up
the fresh material.

`GET /recap?date=…` renders a small page (`web/templates/recap.html`) combining
the day's temp min/max + the timelapse video. Links on the camera tile point at
today's `/recap` and `/timelapse`.

### Configuration & secrets

- **Token redaction.** The full `rtsps_url` (including its token) lives only in
  the gitignored `state/camera.json`. Never paste it in committed code, the README,
  or commit messages.
- **No env vars.** The whole subsystem is configured via `state/camera.json` —
  no `INTEX_CAMERA_*` env, matching the user's "single config source" preference.

## Next steps (discussed, not built)

Shelly EM for true heater state + energy; Pushcut alerts on `Exx` / low-temp; pre-emptive
floor bump before a forecast cold night (the weather plumbing is already in place).
