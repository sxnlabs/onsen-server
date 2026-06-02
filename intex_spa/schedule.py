"""Scheduler config + pure decision engine.

`evaluate()` is a pure function: given the config, the current time, the current
water temp, and an estimated heat rate, it returns the desired {setpoint, heater,
filter} the spa should be at *right now*. The async reconciler (scheduler.py) applies
it. Keeping the policy pure makes every rule unit-testable without a clock or a spa.

Config shape (state/schedule.json) — days are 0=Mon … 6=Sun:

    {
      "enabled": false,
      "eco_temp": 30,
      "heat_rate_c_per_h": 1.0,   # base estimate; refined from history
      "heat_rules":     [{"days": [...], "time": "HH:MM", "temp": 38}],   # thermostat schedule
      "filter_windows": [{"days": [...], "start": "HH:MM", "end": "HH:MM"}],
      "ready_by":       [{"days": [...], "time": "HH:MM", "temp": 38}]    # pre-heat to be ready
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

from . import protocol

DEFAULT_CONFIG: dict = {
    "enabled": False,
    "eco_temp": 30,
    "heat_rate_c_per_h": 1.0,
    "heat_rules": [],
    "filter_windows": [],
    "ready_by": [],
}

MAX_PREHEAT_HOURS = 12.0  # cap pre-heat lead so a slow rate can't heat indefinitely


@dataclass
class Desired:
    """What the spa should be at now. None = scheduler doesn't care about that field."""

    setpoint: int | None = None
    heater: bool | None = None
    filter: bool | None = None
    power: bool | None = None
    reasons: list[str] = field(default_factory=list)


# -- parsing / validation -----------------------------------------------------
def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _in_window(now_t: time, start: str, end: str) -> bool:
    """True if now_t is within [start, end), supporting windows that wrap midnight."""
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s <= e:
        return s <= now_t < e
    return now_t >= s or now_t < e  # wraps midnight (e.g. 22:00 -> 06:00)


def validate_config(cfg: dict) -> dict:
    """Return a normalized config (defaults filled in). Raises ValueError on bad data."""
    out = {**DEFAULT_CONFIG, **(cfg or {})}

    def _check_days(days):
        if not isinstance(days, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
            raise ValueError(f"days must be a list of ints 0..6, got {days!r}")

    def _check_temp(t):
        if not (protocol.TEMP_MIN_C <= int(t) <= protocol.TEMP_MAX_C):
            raise ValueError(f"temp {t} out of range [{protocol.TEMP_MIN_C},{protocol.TEMP_MAX_C}]")

    for r in out["heat_rules"]:
        _check_days(r["days"]); parse_hhmm(r["time"]); _check_temp(r["temp"])
    for w in out["filter_windows"]:
        _check_days(w["days"]); parse_hhmm(w["start"]); parse_hhmm(w["end"])
    for r in out["ready_by"]:
        _check_days(r["days"]); parse_hhmm(r["time"]); _check_temp(r["temp"])
    _check_temp(out["eco_temp"])
    if float(out["heat_rate_c_per_h"]) <= 0:
        raise ValueError("heat_rate_c_per_h must be > 0")
    # keep only known keys
    return {k: out[k] for k in DEFAULT_CONFIG}


def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_CONFIG)
    try:
        return validate_config(json.loads(p.read_text()))
    except (json.JSONDecodeError, ValueError, KeyError):
        return dict(DEFAULT_CONFIG)


def save_config(path: str | Path, cfg: dict) -> dict:
    cfg = validate_config(cfg)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(p)
    return cfg


# -- decision engine ----------------------------------------------------------
def _active_setpoint(heat_rules: list, now: datetime, eco: int) -> tuple[int, dict]:
    """The temp from the most recent heat-rule event at or before `now` (wraps days).

    Returns `(temp, reason)` where `reason` is a structured dict — the UI layer
    translates and formats it. See module docstring for the reason shapes.
    """
    best_dt: datetime | None = None
    best_temp = eco
    for back in range(8):
        day = (now - timedelta(days=back)).date()
        wd = (now - timedelta(days=back)).weekday()
        for r in heat_rules:
            if wd in r["days"]:
                cand = datetime.combine(day, parse_hhmm(r["time"]))
                if cand <= now and (best_dt is None or cand > best_dt):
                    best_dt, best_temp = cand, int(r["temp"])
    if best_dt is None:
        return eco, {"kind": "setpoint_eco", "temp": eco}
    return best_temp, {
        "kind": "setpoint_rule",
        "temp": best_temp,
        "rule_at": best_dt.strftime("%a %H:%M"),
    }


def _ready_by_setpoint(ready_by, now, current_temp, rate):
    """If we're inside a pre-heat window, the temp to aim for (else None, {})."""
    best = None
    reason: dict = {}
    for rb in ready_by:
        if now.weekday() not in rb["days"]:
            continue
        target_dt = datetime.combine(now.date(), parse_hhmm(rb["time"]))
        if now > target_dt:
            continue
        gap = (rb["temp"] - current_temp) if current_temp is not None else None
        if gap is not None and gap <= 0:
            continue  # already warm enough
        lead_h = min((gap / rate) if (gap and rate > 0) else 2.0, MAX_PREHEAT_HOURS)
        start = target_dt - timedelta(hours=lead_h)
        if start <= now <= target_dt and (best is None or rb["temp"] > best):
            best = int(rb["temp"])
            reason = {"kind": "preheat", "temp": best, "for_time": rb["time"]}
    return best, reason


def evaluate(
    cfg: dict,
    now: datetime,
    current_temp: int | None,
    heat_rate: float | None = None,
) -> Desired:
    """Pure: compute the desired spa state for `now`."""
    if not cfg.get("enabled"):
        return Desired(reasons=[{"kind": "scheduler_disabled"}])

    reasons: list[dict] = []
    rate = heat_rate or float(cfg.get("heat_rate_c_per_h", 1.0))

    if current_temp is None:
        return Desired(
            heater=False,
            reasons=[{"kind": "sensor_error", "field": "current_temp"}],
        )

    setpoint, why = _active_setpoint(cfg["heat_rules"], now, int(cfg["eco_temp"]))
    reasons.append(why)

    rb_temp, rb_why = _ready_by_setpoint(cfg["ready_by"], now, current_temp, rate)
    if rb_temp is not None and rb_temp > setpoint:
        setpoint = rb_temp
        reasons.append(rb_why)

    # call for heat while below setpoint; let the spa's thermostat hold at setpoint
    call_for_heat = current_temp is None or current_temp < setpoint
    heater = call_for_heat
    if call_for_heat:
        reasons.append({"kind": "heating", "current": current_temp, "target": setpoint})
    else:
        reasons.append({"kind": "at_setpoint", "target": setpoint})

    in_filter = any(
        now.weekday() in w["days"] and _in_window(now.time(), w["start"], w["end"])
        for w in cfg["filter_windows"]
    )
    if cfg["filter_windows"] or heater:
        filt: bool | None = in_filter or heater  # heating needs circulation
        reasons.append({"kind": "filter_on" if filt else "filter_off"})
    else:
        filt = None  # user didn't configure filtration and not heating: don't manage it

    power = True if (heater or filt) else None

    return Desired(setpoint=setpoint, heater=heater, filter=filt, power=power, reasons=reasons)


# -- heat-rate learning -------------------------------------------------------
def estimate_heat_rate(points: list[dict], default: float = 1.0) -> float:
    """Estimate °C/hour from history rising segments (heater on, temp increasing).

    points: list of {t, cur, set, heat}. Returns median of per-segment rates, or
    `default` when there isn't enough signal.
    """
    rates: list[float] = []
    seg_start = None  # (t, cur)
    for p in points:
        if p.get("heat") and p.get("cur") is not None:
            if seg_start is None:
                seg_start = (p["t"], p["cur"])
            else:
                dt_h = (p["t"] - seg_start[0]) / 3600.0
                dc = p["cur"] - seg_start[1]
                # require ≥2 °C of rise: with 1 °C sensor resolution a single +1 °C step
                # over a short window reads as a wildly inflated rate (quantization noise)
                if dt_h >= 0.25 and dc >= 2:
                    rates.append(dc / dt_h)
                    seg_start = (p["t"], p["cur"])
        else:
            seg_start = None
    if not rates:
        return default
    rates.sort()
    return round(rates[len(rates) // 2], 2)


# -- weather-aware heat-rate model --------------------------------------------
# The spa loses heat ∝ (water − outside air), so the achievable climb rate falls as
# it gets colder outside. We learn two coefficients from the spa's own history:
#   heating:  r_net  = r_gross − k_loss·(water − air)   (°C/h while the heater runs)
#   cooling:  r_cool = k_loss·(water − air)             (°C/h while it's off)
# Until enough air-stamped history exists, we fall back to a gentle, explainable
# cold-weather derate of the measured base rate.
MIN_RATE = 0.2          # never let the effective rate drop to zero (avoids ∞ lead)
MAX_RATE = 2.0          # physical ceiling: ~2.2 kW into ~1100 L ≈ 1.7 °C/h (+margin for
                        # ambient gain). Anything above this is sensor/quantization noise.
MIN_SEGMENTS = 3        # samples needed on each side before trusting calibration
COLD_REF_C = 15.0       # at/above this outside temp, no derate
COLD_SENS = 0.025       # fractional rate loss per °C below COLD_REF_C
DERATE_FLOOR = 0.5      # derate never cuts the rate by more than half


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _segments(points: list[dict]):
    """Yield (heat, dt_h, dT, gap) for consecutive same-heat pairs that carry air."""
    for a, b in zip(points, points[1:]):
        if a.get("cur") is None or b.get("cur") is None:
            continue
        if a.get("air") is None or b.get("air") is None:
            continue
        if bool(a.get("heat")) != bool(b.get("heat")):
            continue
        dt_h = (b["t"] - a["t"]) / 3600.0
        if not (0 < dt_h <= 0.5):
            continue
        dT = b["cur"] - a["cur"]
        gap = (a["cur"] + b["cur"]) / 2.0 - (a["air"] + b["air"]) / 2.0
        yield bool(a.get("heat")), dt_h, dT, gap


def calibrate_rates(points: list[dict], *, min_segments: int = MIN_SEGMENTS):
    """Learn (r_gross, k_loss) from air-stamped history. None if too little signal."""
    loss: list[float] = []   # cooling °C/h per °C of gap (heater off, falling)
    heat: list[tuple[float, float]] = []  # (r_net, gap) heater on, rising
    for is_heat, dt_h, dT, gap in _segments(points):
        if is_heat:
            if dT > 0:
                heat.append((dT / dt_h, gap))
        else:
            cool = -dT / dt_h
            if cool > 0 and gap > 0:
                loss.append(cool / gap)
    if len(loss) < min_segments or len(heat) < min_segments:
        return None
    k_loss = _median(loss)
    if k_loss <= 0:
        return None
    r_gross = _median([rn + k_loss * gap for rn, gap in heat])
    if r_gross <= 0:
        return None
    return round(r_gross, 3), round(k_loss, 4)


def _clamp_rate(r: float) -> float:
    return round(min(MAX_RATE, max(MIN_RATE, r)), 3)


def predict_heat_rate(water: float, air: float, r_gross: float, k_loss: float,
                      *, floor: float = MIN_RATE) -> float:
    return _clamp_rate(r_gross - k_loss * (water - air))


def effective_heat_rate(points: list[dict], air: float | None, *,
                        water: float | None = None, default: float = 1.0):
    """Best achievable °C/h right now given the outside air, plus an explain dict.

    Prefers the self-calibrated physical model; else a cold-weather derate of the
    measured base rate; else (no weather) the plain base rate. Always clamped to a
    physical ceiling (MAX_RATE). The explain dict is surfaced in the UI so the
    behaviour is legible.
    """
    base = _clamp_rate(estimate_heat_rate(points, default=default))
    explain: dict = {"base": base, "air": air, "max": MAX_RATE,
                     "source": "measured", "effective": base}
    if air is None:
        return base, explain

    if water is None and points and points[-1].get("cur") is not None:
        water = points[-1]["cur"]

    coeffs = calibrate_rates(points)
    if coeffs is not None and water is not None:
        r_gross, k_loss = coeffs
        rate = predict_heat_rate(water, air, r_gross, k_loss)  # already clamped
        # k_loss kept at 4 decimals internally for the model; rounded to 2 for
        # display in the explain dict so the UI doesn't print 11.7287.
        explain.update(source="calibrated", r_gross=r_gross, k_loss=round(k_loss, 2),
                       water=water, effective=rate)
        return rate, explain

    factor = max(DERATE_FLOOR, min(1.0, 1.0 - COLD_SENS * max(0.0, COLD_REF_C - air)))
    rate = _clamp_rate(base * factor)
    explain.update(source="weather-derate", factor=round(factor, 3),
                   cold_ref=COLD_REF_C, effective=rate)
    return rate, explain


def _fmt_duration(minutes: int) -> str:
    """Compact, language-neutral duration: '45 min', '1 h', '1 h 20'."""
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h" if m == 0 else f"{h} h {m:02d}"


def eta_to_setpoint(
    now: datetime, current_temp: int | None, target_temp: int | None, rate: float | None
):
    """Pure: estimate when the water reaches `target_temp` climbing at `rate` °C/h.

    Returns a dict {at, label, minutes, hours, over_cap} for the UI, or None when
    there's nothing to predict (no reading, already at/above target, no positive
    rate). `over_cap` flags an estimate beyond MAX_PREHEAT_HOURS — a cold-weather
    crawl the UI should present as "very slow" rather than a precise clock time.
    """
    if current_temp is None or target_temp is None or not rate or rate <= 0:
        return None
    gap = target_temp - current_temp
    if gap <= 0:
        return None  # already there — the heater is holding, not climbing
    hours = gap / rate
    minutes = int(round(hours * 60))
    eta_dt = now + timedelta(hours=hours)
    # prefix the weekday when it lands on another day, so HH:MM isn't ambiguous
    at = eta_dt.strftime("%H:%M" if eta_dt.date() == now.date() else "%a %H:%M")
    return {
        "at": at,
        "label": _fmt_duration(minutes),
        "minutes": minutes,
        "hours": round(hours, 1),
        "over_cap": hours > MAX_PREHEAT_HOURS,
    }


def next_preheat(cfg: dict, now: datetime, current_temp: int | None, rate: float):
    """For display: the soonest upcoming ready-by today and when pre-heat would start."""
    best = None
    for rb in cfg.get("ready_by", []):
        if now.weekday() not in rb["days"]:
            continue
        target_dt = datetime.combine(now.date(), parse_hhmm(rb["time"]))
        if now > target_dt:
            continue
        gap = (rb["temp"] - current_temp) if current_temp is not None else None
        if gap is not None and gap <= 0:
            continue
        lead_h = min((gap / rate) if (gap and rate > 0) else 2.0, MAX_PREHEAT_HOURS)
        start = target_dt - timedelta(hours=lead_h)
        cand = {
            "time": rb["time"],
            "temp": int(rb["temp"]),
            "lead_h": round(lead_h, 1),
            "start": start.strftime("%H:%M"),
            "active": start <= now <= target_dt,
        }
        if best is None or target_dt < datetime.combine(now.date(), parse_hhmm(best["time"])):
            best = cand
    return best
