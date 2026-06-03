"""Outside-weather client (Open-Meteo) — cached, dependency-free, fail-soft.

Drives the scheduler's pre-heat lead: the spa loses heat (and so heats slower)
roughly in proportion to (water − outside air), so a cold/windy forecast means
the "ready by" window must start earlier. We fetch the hourly forecast for the
configured location, cache it (in memory + on disk so we survive restarts and
don't hammer the API), and expose pure interpolation helpers the scheduler
reads each tick.

No third-party HTTP dependency: the blocking `urllib` fetch runs in a worker
thread so it never stalls the event loop. Every network path is best-effort —
on any error we keep the last good forecast (stale-but-useful) and the caller
falls back to the weather-agnostic heat-rate estimate.

Location is configurable via `WEATHER_LAT` / `WEATHER_LON` env vars; defaults
below point at Brest, France (the author's location — change them for yours).

Source: https://open-meteo.com/ (free, no API key).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

_LOG = logging.getLogger("intex_spa.weather")

# Defaults — override with WEATHER_LAT / WEATHER_LON env vars in the LaunchAgent.
DEFAULT_LAT = 48.45
DEFAULT_LON = -4.42
_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherClient:
    """Cached hourly forecast with pure read helpers.

    `refresh()` is async and networked (cheap when the cache is warm); the read
    helpers (`air_at`, `air_now`, `air_window`, `low_ahead`, `snapshot`) are pure and
    instant — safe to call from the poll loop. All temperatures are the real outside
    air (`temperature_2m`, °C); the apparent ("feels-like") temp and wind are kept too
    for display.
    """

    def __init__(
        self,
        lat: float = DEFAULT_LAT,
        lon: float = DEFAULT_LON,
        *,
        cache_path: str | Path | None = "state/weather.json",
        ttl: float = 1800.0,      # 30 min between fetches
        timeout: float = 10.0,
        url_base: str = _URL,
    ) -> None:
        self.lat = lat
        self.lon = lon
        self.cache_path = Path(cache_path) if cache_path else None
        self.ttl = ttl
        self.timeout = timeout
        self.url_base = url_base
        self._hours: list[dict] = []   # [{"t","air","feels","wind","code"}], sorted by t
        self._sun: list[dict] = []     # [{"sunrise","sunset"}], sorted by sunrise
        self._fetched_at: float = 0.0
        self._load()

    # -- fetch / cache --------------------------------------------------------
    async def refresh(self, *, now: float | None = None, force: bool = False) -> bool:
        """Refresh if stale. Returns True iff a network fetch actually happened."""
        now = time.time() if now is None else now
        if not force and self._hours and (now - self._fetched_at) < self.ttl:
            return False
        try:
            payload = await asyncio.to_thread(self._fetch_blocking)
            hours = self._parse(payload)
            if hours:
                self._hours = hours
                self._sun = self._parse_sun(payload)
                self._fetched_at = now
                self._save()
                return True
            _LOG.warning("weather: empty forecast, keeping previous")
        except Exception:  # noqa: BLE001 — best-effort; keep stale data
            _LOG.warning("weather: fetch failed, keeping previous forecast", exc_info=True)
        return False

    def _fetch_blocking(self) -> dict:
        q = urllib.parse.urlencode(
            {
                "latitude": self.lat,
                "longitude": self.lon,
                "hourly": "temperature_2m,apparent_temperature,wind_speed_10m,weather_code",
                "daily": "sunrise,sunset",
                "forecast_days": 2,
                "timeformat": "unixtime",
                "timezone": "GMT",
            }
        )
        req = urllib.request.Request(f"{self.url_base}?{q}", headers={"User-Agent": "intex-spa/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 (https only)
            return json.loads(r.read().decode("utf-8"))

    @staticmethod
    def _parse(payload: dict) -> list[dict]:
        h = (payload or {}).get("hourly") or {}
        ts = h.get("time") or []
        air = h.get("temperature_2m") or []
        feels = h.get("apparent_temperature") or []
        wind = h.get("wind_speed_10m") or []
        code = h.get("weather_code") or []
        out: list[dict] = []
        for i, t in enumerate(ts):
            if i >= len(air) or air[i] is None:
                continue
            out.append(
                {
                    "t": float(t),
                    "air": float(air[i]),
                    "feels": float(feels[i]) if i < len(feels) and feels[i] is not None else None,
                    "wind": float(wind[i]) if i < len(wind) and wind[i] is not None else None,
                    "code": int(code[i]) if i < len(code) and code[i] is not None else None,
                }
            )
        out.sort(key=lambda p: p["t"])
        return out

    @staticmethod
    def _parse_sun(payload: dict) -> list[dict]:
        d = (payload or {}).get("daily") or {}
        sunrise = d.get("sunrise") or []
        sunset = d.get("sunset") or []
        out: list[dict] = []
        for i, sr in enumerate(sunrise):
            if i >= len(sunset) or sr is None or sunset[i] is None:
                continue
            out.append({"sunrise": float(sr), "sunset": float(sunset[i])})
        out.sort(key=lambda p: p["sunrise"])
        return out

    def _load(self) -> None:
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text())
            self._hours = data.get("hours") or []
            self._sun = data.get("sun") or []
            self._fetched_at = float(data.get("fetched_at") or 0.0)
            if self._hours and (
                "code" not in self._hours[0] or not self._sun
            ):
                # Older caches predate UI sky state. Refresh immediately.
                self._fetched_at = 0.0
        except (json.JSONDecodeError, ValueError, OSError):
            pass

    def _save(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps({
                "fetched_at": self._fetched_at,
                "hours": self._hours,
                "sun": self._sun,
            }))
            tmp.replace(self.cache_path)
        except OSError:
            _LOG.warning("weather: could not persist cache", exc_info=True)

    # -- pure read helpers ----------------------------------------------------
    def air_at(self, when: float, key: str = "air") -> float | None:
        """Linearly interpolate the outside value at epoch `when` (None if no data)."""
        pts = [p for p in self._hours if p.get(key) is not None]
        if not pts:
            return None
        if when <= pts[0]["t"]:
            return pts[0][key]
        if when >= pts[-1]["t"]:
            return pts[-1][key]
        for a, b in zip(pts, pts[1:]):
            if a["t"] <= when <= b["t"]:
                span = b["t"] - a["t"]
                if span <= 0:
                    return a[key]
                frac = (when - a["t"]) / span
                return round(a[key] + frac * (b[key] - a[key]), 2)
        return pts[-1][key]

    def air_now(self, now: float | None = None) -> float | None:
        return self.air_at(time.time() if now is None else now)

    def code_at(self, when: float) -> int | None:
        pts = [p for p in self._hours if p.get("code") is not None]
        if not pts:
            return None
        closest = min(pts, key=lambda p: abs(p["t"] - when))
        return int(closest["code"])

    @staticmethod
    def condition_for_code(code: int | None) -> str | None:
        if code is None:
            return None
        return "clear" if code in {0, 1} else "cloudy"

    def air_window(self, start: float, end: float, key: str = "air") -> float | None:
        """Mean outside value over [start, end] (interpolates the endpoints)."""
        if end < start:
            start, end = end, start
        samples = [self.air_at(start, key), self.air_at(end, key)]
        samples += [p[key] for p in self._hours if start < p["t"] < end and p.get(key) is not None]
        vals = [s for s in samples if s is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 2)

    def low_ahead(self, hours: float = 12.0, now: float | None = None, key: str = "air") -> float | None:
        """Minimum forecast value over the next `hours` (e.g. tonight's low)."""
        now = time.time() if now is None else now
        vals = [p[key] for p in self._hours if now <= p["t"] <= now + hours * 3600 and p.get(key) is not None]
        if not vals:
            v = self.air_at(now, key)
            return v
        return round(min(vals), 2)

    def sun_window(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        if not self._sun:
            return None
        return min(
            self._sun,
            key=lambda p: abs(((p["sunrise"] + p["sunset"]) / 2.0) - now),
        )

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        code = self.code_at(now)
        sun = self.sun_window(now) or {}
        return {
            "source": "open-meteo",
            "lat": self.lat,
            "lon": self.lon,
            "air": self.air_now(now),
            "feels": self.air_at(now, "feels"),
            "wind": self.air_at(now, "wind"),
            "weather_code": code,
            "condition": self.condition_for_code(code),
            "sunrise": sun.get("sunrise"),
            "sunset": sun.get("sunset"),
            "low_12h": self.low_ahead(12.0, now),
            "fetched_at": self._fetched_at or None,
            "age_s": round(now - self._fetched_at, 1) if self._fetched_at else None,
            "hours": len(self._hours),
        }
