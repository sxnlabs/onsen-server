"""FastAPI app: one process, one supervisor, one TCP connection to the spa.

Run with uvicorn's factory mode (default single in-process server; never pass
--workers, or you'd get a process manager around the one spa socket):

    INTEX_SPA_HOST=<spa-ip> uvicorn web.main:make_app --factory

The UI is HTMX + the SSE extension (assets vendored under static/vendor): the panel
re-renders on every state push from the supervisor's poll loop; buttons POST commands
that return the same partial. A Chart.js graph reads /history.

Optional auth: set HERMES_PASSWORD to gate the UI behind a signed-cookie login. This
protects the web UI only — NOT the spa's open TCP port (lock that down at the firewall).

HTTP concurrency is async in the single uvicorn process. Blocking side work uses a
bounded I/O thread pool, so camera/weather/timelapse tasks don't stall the API.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json as _json
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from intex_spa import camera as cam_mod
from intex_spa import protocol
from intex_spa.client import SpaUnreachable
from intex_spa.command_log import CommandLog
from intex_spa.concurrency import configure_io_threads, reset_io_threads, run_blocking
from intex_spa.history import TempHistory
from intex_spa import schedule as sched_mod
from intex_spa.scheduler import Scheduler
from intex_spa.supervisor import Supervisor
from intex_spa.weather import DEFAULT_LAT, DEFAULT_LON, WeatherClient

from . import auth, i18n

_BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))


def _asset_version() -> str:
    """Short content hash of the local (non-vendored) static assets, appended to
    their URLs as ?v= so a redeploy isn't masked by a stale browser/CDN copy
    (the app serves /static without a Cache-Control header)."""
    h = hashlib.sha256()
    for name in ("app.css", "schedule.js", "camera.js"):
        try:
            h.update((_BASE / "static" / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


_ASSET_V = _asset_version()

# functions exposed in the UI for this model (Baltik: no jets, no sanitizer).
# The middle element is a *translation key*; the template renders it through t().
UI_TOGGLES = [
    ("power", "toggle.power", "⚡"),
    ("heater", "toggle.heater", "🔥"),
    ("filter", "toggle.filter", "🌀"),
    ("bubbles", "toggle.bubbles", "🫧"),
]
UI_TOGGLE_FIELDS = {field for field, _label, _icon in UI_TOGGLES}


def _fmt_ts(epoch: float | None) -> str:
    if not epoch:
        return ""
    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


templates.env.filters["ts"] = _fmt_ts
templates.env.globals["ui_toggles"] = UI_TOGGLES
templates.env.globals["LANGS"] = i18n.LANGS


def _client_ip(request: Request) -> str:
    """Best-effort client IP, honoring the proxy chain (Scaleway LB → kamal-proxy)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _forwarded_https(request: Request) -> bool:
    """True when the original client reached the edge over HTTPS (so cookies can be Secure)."""
    proto = request.headers.get("x-forwarded-proto", "")
    return proto.split(",")[0].strip() == "https" or request.url.scheme == "https"


def _intervals_from_points(
    points: list[dict],
    key: str,
    start: float,
    end: float,
) -> list[dict]:
    intervals: list[dict] = []
    active = False
    active_start = start
    for p in points:
        if key not in p:
            continue
        try:
            t = float(p["t"])
        except (KeyError, TypeError, ValueError):
            continue
        value = bool(p.get(key))
        if value and not active:
            active = True
            active_start = max(start, t)
        elif not value and active:
            intervals.append({"start": active_start, "end": min(end, t)})
            active = False
    if active:
        intervals.append({"start": active_start, "end": end})
    return [i for i in intervals if i["end"] > i["start"]]


def _intervals_from_command_log(
    path: str | Path | None,
    fields: set[str],
    start: float,
    end: float,
    extra_events: dict[str, list[tuple[float, bool]]] | None = None,
) -> dict[str, list[dict]]:
    out = {field: [] for field in fields}
    events: list[tuple[float, int, str, bool]] = []
    for field, field_events in (extra_events or {}).items():
        if field not in fields:
            continue
        for t, value in field_events:
            if start <= t <= end:
                events.append((t, 1, field, value))
    if path is None:
        return _intervals_from_transition_events(out, events, start, end, fields)
    p = Path(path)
    if not p.exists():
        return _intervals_from_transition_events(out, events, start, end, fields)

    before: dict[str, bool | None] = {field: None for field in fields}
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return _intervals_from_transition_events(out, events, start, end, fields)

    for line in lines:
        try:
            row = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if row.get("kind") != "toggle" or row.get("field") not in fields:
            continue
        after = row.get("after")
        field = row["field"]
        if not isinstance(after, dict) or field not in after:
            continue
        try:
            t = float(row["t"])
        except (KeyError, TypeError, ValueError):
            continue
        value = bool(after[field])
        if t < start:
            before[field] = value
        elif t <= end:
            events.append((t, 0, field, value))

    return _intervals_from_transition_events(out, events, start, end, fields, before)


def _intervals_from_transition_events(
    out: dict[str, list[dict]],
    events: list[tuple[float, int, str, bool]],
    start: float,
    end: float,
    fields: set[str],
    before: dict[str, bool | None] | None = None,
) -> dict[str, list[dict]]:
    before = before or {field: None for field in fields}
    active_since: dict[str, float | None] = {
        field: start if before[field] is True else None for field in fields
    }
    for t, _priority, field, value in sorted(events):
        if value and active_since[field] is None:
            active_since[field] = max(start, t)
        elif not value and active_since[field] is not None:
            out[field].append({"start": active_since[field], "end": min(end, t)})
            active_since[field] = None
    for field, since in active_since.items():
        if since is not None:
            out[field].append({"start": since, "end": end})
    return out


def _events_from_points(points: list[dict], key: str) -> list[tuple[float, bool]]:
    events: list[tuple[float, bool]] = []
    for p in points:
        if key not in p:
            continue
        try:
            events.append((float(p["t"]), bool(p[key])))
        except (KeyError, TypeError, ValueError):
            continue
    return events


def _merge_intervals(*groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    intervals = sorted(
        (
            {"start": float(i["start"]), "end": float(i["end"])}
            for group in groups
            for i in group
            if i.get("end") is not None and i.get("start") is not None
        ),
        key=lambda i: i["start"],
    )
    for interval in intervals:
        if interval["end"] <= interval["start"]:
            continue
        if not merged or interval["start"] > merged[-1]["end"]:
            merged.append(interval)
        else:
            merged[-1]["end"] = max(merged[-1]["end"], interval["end"])
    return merged


def _history_activity(
    points: list[dict],
    command_log_path: str | Path | None,
) -> dict[str, list[dict]]:
    if not points:
        return {"heater": [], "filter": []}
    start = float(points[0]["t"])
    end = float(points[-1]["t"])
    from_points = {
        "heater": _intervals_from_points(points, "heat", start, end),
        "filter": _intervals_from_points(points, "filter", start, end),
    }
    from_commands = _intervals_from_command_log(
        command_log_path,
        {"heater", "filter"},
        start,
        end,
        extra_events={
            "heater": _events_from_points(points, "heat"),
            "filter": _events_from_points(points, "filter"),
        },
    )
    return {
        "heater": _merge_intervals(from_points["heater"], from_commands["heater"]),
        "filter": _merge_intervals(from_points["filter"], from_commands["filter"]),
    }


def create_app(
    host: str,
    *,
    port: int = protocol.PORT,
    poll_interval: float = 10.0,
    history_path: str | None = "state/history.jsonl",
    password: str | None = None,
    secret_path: str = "state/.secret",
    schedule_path: str | None = "state/schedule.json",
    weather_enabled: bool = True,
    weather_lat: float = DEFAULT_LAT,
    weather_lon: float = DEFAULT_LON,
    weather_cache_path: str | None = "state/weather.json",
    camera_config_path: str | None = "state/camera.json",
    command_log_path: str | None = "state/commands.jsonl",
    manual_override_path: str | None = "state/manual_overrides.json",
    pause_path: str | None = "state/pause.json",
    automation_cooldown_path: str | None = "state/automation_cooldowns.json",
    login_limiter: auth.GlobalRateLimiter | None = None,
    io_threads: int | None = 8,
) -> FastAPI:
    weather = (
        WeatherClient(weather_lat, weather_lon, cache_path=weather_cache_path)
        if weather_enabled
        else None
    )
    history = TempHistory(path=history_path)
    command_log = CommandLog(command_log_path)
    supervisor = Supervisor(
        host,
        port=port,
        poll_interval=poll_interval,
        history=history,
        air_provider=(weather.air_now if weather else None),
        command_log=command_log,
        pause_path=pause_path,
    )
    scheduler = Scheduler(
        supervisor,
        config_path=schedule_path,
        weather=weather,
        override_path=manual_override_path,
        cooldown_path=automation_cooldown_path,
    )
    secret = auth.load_or_create_secret(secret_path) if password else b""
    login_throttle = auth.LoginThrottle()
    login_limiter = login_limiter or auth.GlobalRateLimiter()

    # -- camera subsystem (master switch via state/camera.json) ----------
    # weather pattern: instantiate once, hold None when unconfigured; every
    # route checks `camera is None` and degrades cleanly. No env vars.
    camera_config = cam_mod.load_config(camera_config_path)
    if camera_config is not None:
        camera = cam_mod.CameraSnapshot(
            camera_config["rtsps_url"],
            frame_path=camera_config["frame_path"],
            history_dir=camera_config["history_dir"],
            poll_seconds=camera_config["poll_seconds"],
            timelapse_every_seconds=camera_config["timelapse_every_seconds"],
            timelapse_retention_days=camera_config["timelapse_retention_days"],
            timelapse_fps=camera_config["timelapse_fps"],
            jpeg_quality=camera_config["jpeg_quality"],
            snapshot_max_width=camera_config["snapshot_max_width"],
            ffmpeg_bin=camera_config["ffmpeg"],
            ffmpeg_extra_args=camera_config["ffmpeg_extra_args"],
        )
    else:
        camera = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        configure_io_threads(io_threads)
        weather_warmup: asyncio.Task | None = None
        try:
            if weather is not None:
                # warm the forecast in the background — never block startup on the network
                # (the scheduler tick also refreshes; air_now() returns None until it lands)
                weather_warmup = asyncio.create_task(weather.refresh(force=True))
            await supervisor.start()
            await scheduler.start()
            if camera is not None:
                await camera.start()
            yield
        finally:
            if weather_warmup is not None:
                weather_warmup.cancel()
                with suppress(asyncio.CancelledError):
                    await weather_warmup
            if camera is not None:
                await camera.stop()
            await scheduler.stop()
            await supervisor.stop()
            reset_io_threads()

    app = FastAPI(title="Intex Spa", lifespan=lifespan)
    app.state.supervisor = supervisor
    app.state.scheduler = scheduler
    app.state.weather = weather
    app.state.camera = camera
    app.state.camera_config = camera_config
    app.state.camera_config_path = camera_config_path
    app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")

    if password:

        @app.middleware("http")
        async def _gate(request: Request, call_next):
            path = request.url.path
            public = path == "/login" or path.startswith("/static/") or path == "/healthz"
            if not public and not auth.token_valid(request.cookies.get(auth.COOKIE_NAME), secret):
                if request.method == "GET":
                    return RedirectResponse("/login", status_code=303)
                return PlainTextResponse("authentication required", status_code=401)
            return await call_next(request)

    # Lang middleware. Registered LAST so it executes FIRST (FastAPI runs
    # middlewares in reverse registration order). `?lang=` is consumed here:
    # we store it in a cookie and redirect to the same URL without the param —
    # bookmarks and shared links stay clean.
    @app.middleware("http")
    async def _lang_mw(request: Request, call_next):
        q_lang = request.query_params.get("lang")
        if q_lang in i18n.LANGS:
            kept = {k: v for k, v in request.query_params.multi_items() if k != "lang"}
            from urllib.parse import urlencode
            qs = ("?" + urlencode(kept, doseq=True)) if kept else ""
            resp = RedirectResponse(f"{request.url.path}{qs}", status_code=303)
            resp.set_cookie("lang", q_lang, max_age=60 * 60 * 24 * 365, httponly=False, samesite="lax")
            return resp
        request.state.lang = i18n.detect(
            request.cookies.get("lang"),
            request.headers.get("accept-language"),
        )
        return await call_next(request)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        # Defense-in-depth headers for the internet-exposed UI. Registered last
        # ⇒ outermost ⇒ also stamps the gate's redirect/401 responses. HSTS is
        # honored only over HTTPS (the public path via the Scaleway LB).
        response = await call_next(request)
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def _ctx(request: Request, **extra) -> dict:
        """Template context with lang + per-render closures.

        `t` and `i18n_bundle_json` are closures bound to the current request's
        lang. We pass them in the render context rather than as Jinja globals
        because Starlette's `Jinja2Templates` doesn't always propagate
        `@pass_context`-decorated globals (resolution comes up "undefined").
        Per-request closures sidestep that entirely.
        """
        lang = getattr(request.state, "lang", i18n.DEFAULT_LANG)
        def _t(key: str, **params: object) -> str:
            return i18n.t(lang, key, **params)
        def _bundle_json() -> str:
            return _json.dumps(i18n.bundle(lang))
        return {
            "lang": lang,
            "t": _t,
            "i18n_bundle_json": _bundle_json,
            "asset_v": _ASSET_V,
            **extra,
        }

    def _panel_eta(state: dict | None):
        """ETA to the live setpoint for the panel — only while actively heating."""
        st = (state or {}).get("status") or {}
        if not st.get("heater"):
            return None  # not climbing → no estimate (avoid a misleading time)
        return sched_mod.eta_to_setpoint(
            _dt.datetime.now(),
            st.get("current_temp"),
            st.get("preset_temp"),
            scheduler.current_heat_rate(),
        )

    def _manual_hold_context(t):
        remaining = scheduler.manual_overrides_remaining()
        if not remaining:
            return None
        label_keys = {
            "power": "toggle.power",
            "preset": "panel.consigne",
            "heater": "toggle.heater",
            "filter": "toggle.filter",
        }
        fields = [t(label_keys.get(field, field)) for field in remaining]
        longest = max(remaining.values())
        minutes = max(1, (longest + 59) // 60)
        return {
            "fields": ", ".join(fields),
            "remaining": t("panel.manual_hold_minutes", minutes=minutes),
            "remaining_seconds": longest,
        }

    def _panel_context(request: Request, state: dict | None = None) -> dict:
        state = supervisor.state if state is None else state
        ctx = _ctx(request, s=state, eta=_panel_eta(state))
        ctx["manual_hold"] = _manual_hold_context(ctx["t"])
        return ctx

    def render_panel(request: Request):
        return templates.TemplateResponse(
            request, "_panel.html",
            _panel_context(request),
        )

    def _decimate_points(points: list[dict], max_points: int | None) -> list[dict]:
        """Reduce chart payload while preserving first/last and visible extrema."""
        if not max_points or max_points <= 0 or len(points) <= max_points:
            return points
        if max_points == 1:
            return [points[-1]]
        if max_points == 2:
            return [points[0], points[-1]]
        if max_points == 3:
            return [points[0], points[len(points) // 2], points[-1]]

        buckets = max(1, (max_points - 2) // 2)
        middle = points[1:-1]
        if len(middle) <= buckets:
            return points

        out = [points[0]]
        for i in range(buckets):
            start = round(i * len(middle) / buckets)
            end = round((i + 1) * len(middle) / buckets)
            bucket = middle[start:end]
            if not bucket:
                continue
            vals = [p for p in bucket if p.get("cur") is not None]
            if not vals:
                out.append(bucket[len(bucket) // 2])
                continue
            low = min(vals, key=lambda p: (p["cur"], p["t"]))
            high = max(vals, key=lambda p: (p["cur"], -p["t"]))
            pair = sorted({low["t"]: low, high["t"]: high}.values(), key=lambda p: p["t"])
            out.extend(pair)
        out.append(points[-1])
        return out

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        if not password:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", _ctx(request, error=False))

    @app.post("/login")
    async def login_submit(request: Request):
        ip = _client_ip(request)
        # Gate BEFORE verifying the password so guessing is actually bounded.
        # Per-IP lockout FIRST: an already-locked client must NOT consume a
        # global token (else one locked IP at ~1 req/s keeps the shared bucket
        # empty and 429s everyone). Only a non-locked attempt then takes a global
        # token, which bounds rotated/concurrent sources that evade the per-IP key.
        wait = login_throttle.retry_after(ip) or login_limiter.take()
        if wait > 0:
            resp = templates.TemplateResponse(
                request, "login.html", _ctx(request, error=True), status_code=429
            )
            resp.headers["Retry-After"] = str(wait)
            return resp
        body = (await request.body()).decode("utf-8", "replace")
        supplied = parse_qs(body).get("password", [""])[0]
        if password and auth.password_ok(supplied, password):
            login_throttle.reset(ip)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(
                auth.COOKIE_NAME,
                auth.issue_token(secret),
                max_age=auth.DEFAULT_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=_forwarded_https(request),
            )
            return resp
        login_throttle.record_failure(ip)
        return templates.TemplateResponse(request, "login.html", _ctx(request, error=True), status_code=401)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            _ctx(
                request,
                **_panel_context(request),
                spa_host=host,
                camera_enabled=camera is not None,
            ),
        )

    @app.get("/panel", response_class=HTMLResponse)
    async def panel(request: Request):
        return render_panel(request)

    @app.post("/toggle/{field}", response_class=HTMLResponse)
    async def toggle(request: Request, field: str):
        if field not in UI_TOGGLE_FIELDS:
            raise HTTPException(status_code=404, detail=f"unknown field {field!r}")
        current = bool((supervisor.state.get("status") or {}).get(field))
        try:
            await supervisor.set_field(field, not current)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except SpaUnreachable as e:
            raise HTTPException(status_code=503, detail=str(e))
        scheduler.note_manual(field)  # don't let the scheduler immediately revert
        return render_panel(request)

    @app.post("/preset/{temp}", response_class=HTMLResponse)
    async def preset(request: Request, temp: int):
        try:
            await supervisor.set_preset(temp)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except SpaUnreachable as e:
            raise HTTPException(status_code=503, detail=str(e))
        scheduler.note_manual("preset")
        return render_panel(request)

    @app.get("/history")
    async def history_json(hours: float = 24.0, max_points: int | None = None):
        unit = (supervisor.state.get("status") or {}).get("unit") or "C"
        points = supervisor.history.recent(hours=hours)
        command_log_path = command_log.path if command_log is not None else None
        return {
            "unit": unit,
            "points": _decimate_points(points, max_points),
            "activity": _history_activity(points, command_log_path),
        }

    @app.get("/weather")
    async def weather_json():
        if weather is None:
            return {"enabled": False}
        snap = weather.snapshot()
        snap["enabled"] = True
        # surface the scheduler's latest plan so the UI can explain what it
        # is doing right now (structured reasons + technical rate breakdown).
        plan = scheduler.last_plan or {}
        snap["rate_explain"] = plan.get("rate_explain")
        snap["hysteresis"] = plan.get("hysteresis")
        snap["preheat"] = plan.get("preheat")
        snap["plan"] = {
            "enabled": plan.get("enabled", False),
            "setpoint": plan.get("setpoint"),
            "heater": plan.get("heater"),
            "filter": plan.get("filter"),
            "reasons": plan.get("reasons") or [],
        }
        return snap

    @app.get("/api/schedule")
    async def api_schedule_get():
        return {"config": scheduler.get_config(), "plan": scheduler.last_plan}

    @app.post("/api/schedule")
    async def api_schedule_post(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        try:
            cfg = scheduler.set_config(body)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"invalid config: {e}")
        return {"ok": True, "config": cfg}

    @app.get("/healthz")
    async def healthz():
        s = supervisor.state
        # Public (unauthenticated) endpoint — expose only a coarse boolean, never
        # the raw error string (it embeds the spa's internal tunnel IP/port).
        return {
            "online": s["online"],
            "updated_at": s["updated_at"],
            "error": s["error"] is not None,
            "paused": supervisor.paused,
        }

    @app.get("/api/resume-preview")
    async def api_resume_preview():
        return await scheduler.resume_preview()

    @app.post("/api/pause")
    async def api_pause(state: str | None = None, confirm: str | None = None):
        """Pause or resume the supervisor poll loop + scheduler reconciliation.

        Query string `state`: `on`/`true`/`1` → pause, `off`/`false`/`0` → resume,
        anything else (or absent) → toggle the current state.
        """
        truthy = {"on", "true", "1", "pause", "paused"}
        falsy  = {"off", "false", "0", "resume"}
        if state is None:
            new = not supervisor.paused
        elif state.lower() in truthy:
            new = True
        elif state.lower() in falsy:
            new = False
        else:
            raise HTTPException(status_code=400, detail="state must be on/off (or omit to toggle)")
        if supervisor.paused and not new:
            preview = await scheduler.resume_preview()
            if not preview["can_resume"]:
                raise HTTPException(status_code=409, detail={
                    "message": "cannot resume automation without a fresh online spa status",
                    "preview": preview,
                })
            confirmed = (confirm or "").lower() in {"1", "true", "yes", "confirm"}
            if preview["actions"] and not confirmed:
                raise HTTPException(status_code=409, detail={
                    "message": "resuming automation would send spa commands",
                    "requires_confirm": True,
                    "preview": preview,
                })
        supervisor.set_paused(new)
        return {"paused": supervisor.paused}

    # -- camera endpoints (all degrade to {"enabled": false} when off) ----
    @app.get("/api/camera/status")
    async def camera_status():
        if camera is None:
            return {"enabled": False}
        snap = camera.snapshot()
        snap["enabled"] = True
        return snap

    @app.get("/camera.jpg")
    async def camera_jpg():
        if camera is None or camera.last_frame_at is None:
            raise HTTPException(status_code=404, detail="no frame")
        return FileResponse(
            str(camera.frame_path),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/timelapse")
    async def timelapse(date: str):
        if camera is None:
            raise HTTPException(status_code=503, detail="camera disabled")
        try:
            _dt.datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
        mp4 = await run_blocking(camera.generate_timelapse, date)
        if mp4 is None:
            raise HTTPException(status_code=404, detail=f"no frames for {date}")
        return FileResponse(str(mp4), media_type="video/mp4")

    @app.get("/recap", response_class=HTMLResponse)
    async def recap(request: Request, date: str | None = None):
        if camera is None:
            raise HTTPException(status_code=503, detail="camera disabled")
        d = date or _dt.date.today().isoformat()
        try:
            day = _dt.datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
        # Day window in local epoch seconds (matches history.t)
        start = _dt.datetime.combine(day, _dt.time(0, 0)).timestamp()
        end = start + 86400
        pts = [p for p in supervisor.history.recent(hours=24 * 8)
               if start <= p["t"] < end and p.get("cur") is not None]
        temps = [p["cur"] for p in pts]
        return templates.TemplateResponse(
            request, "recap.html",
            _ctx(
                request,
                date=d,
                min_t=min(temps) if temps else None,
                max_t=max(temps) if temps else None,
                samples=len(pts),
            ),
        )

    @app.get("/events")
    async def events(request: Request):
        async def gen():
            q = supervisor.subscribe()
            try:
                while not await request.is_disconnected():
                    try:
                        state = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield {"event": "ping", "data": ""}
                        continue
                    # SSE renders happen *after* the lang middleware has run on
                    # the original request, so the lang is on request.state.
                    # Build the same closure-based context the regular routes
                    # use (env.globals don't carry `t`).
                    html = templates.env.get_template("_panel.html").render(
                        **_panel_context(request, state)
                    )
                    yield {"event": "update", "data": html}
            finally:
                supervisor.unsubscribe(q)

        return EventSourceResponse(gen())

    return app


def _configured_password() -> str | None:
    """UI password from HERMES_PASSWORD, else state/.password (written by install.sh)."""
    env = os.environ.get("HERMES_PASSWORD")
    if env:
        return env
    pf = Path("state/.password")
    if pf.exists():
        return pf.read_text().strip() or None
    return None


def _configured_io_threads() -> int:
    raw = os.environ.get("HERMES_IO_THREADS", "8")
    try:
        value = int(raw)
    except ValueError:
        return 8
    return max(1, value)


def make_app() -> FastAPI:
    """uvicorn --factory entry point. Reads config from the environment."""
    host = os.environ.get("INTEX_SPA_HOST")
    if not host:
        raise RuntimeError("INTEX_SPA_HOST is required (the spa wifi module IP on your LAN)")
    return create_app(
        host,
        port=int(os.environ.get("INTEX_SPA_PORT", protocol.PORT)),
        poll_interval=float(os.environ.get("INTEX_SPA_POLL", "10")),
        password=_configured_password(),
        weather_enabled=os.environ.get("WEATHER_ENABLED", "1") not in ("0", "false", "no", ""),
        weather_lat=float(os.environ.get("WEATHER_LAT", DEFAULT_LAT)),
        weather_lon=float(os.environ.get("WEATHER_LON", DEFAULT_LON)),
        io_threads=_configured_io_threads(),
    )
