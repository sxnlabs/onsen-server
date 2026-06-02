"""Unit tests for the pure schedule decision engine."""

from datetime import datetime

import pytest

from intex_spa import schedule as S

MON = datetime(2026, 5, 18)  # a Monday (weekday 0)


def mk(**over) -> dict:
    cfg = {**S.DEFAULT_CONFIG, "enabled": True}
    cfg.update(over)
    return S.validate_config(cfg)


def at(h, m=0):
    return MON.replace(hour=h, minute=m)


def test_disabled_does_nothing():
    d = S.evaluate(mk(enabled=False), at(8), current_temp=30)
    assert d.setpoint is None and d.heater is None and d.filter is None
    assert d.reasons[0]["kind"] == "scheduler_disabled"


def test_heat_rule_active_sets_point_and_heater():
    cfg = mk(heat_rules=[{"days": [0, 1, 2, 3, 4, 5, 6], "time": "07:00", "temp": 38}])
    d = S.evaluate(cfg, at(8), current_temp=30)
    assert d.setpoint == 38
    assert d.heater is True       # 30 < 38 -> calling for heat
    assert d.filter is True       # heating needs circulation
    assert d.power is True


def test_heater_off_when_at_or_above_setpoint():
    cfg = mk(heat_rules=[{"days": [0, 1, 2, 3, 4, 5, 6], "time": "07:00", "temp": 30}])
    d = S.evaluate(cfg, at(8), current_temp=32)  # already warmer than target
    assert d.setpoint == 30
    assert d.heater is False


def test_sensor_error_cuts_heat_and_makes_no_other_demands():
    cfg = mk(heat_rules=[{"days": [0, 1, 2, 3, 4, 5, 6], "time": "07:00", "temp": 38}])
    d = S.evaluate(cfg, at(8), current_temp=None)
    assert d.setpoint is None
    assert d.heater is False
    assert d.filter is None
    assert d.power is None
    assert d.reasons == [{"kind": "sensor_error", "field": "current_temp"}]


def test_eco_fallback_when_no_rules():
    cfg = mk(eco_temp=29, heat_rules=[])
    d = S.evaluate(cfg, at(8), current_temp=25)
    assert d.setpoint == 29
    assert d.heater is True  # 25 < 29
    assert any(r["kind"] == "setpoint_eco" for r in d.reasons)


def test_heat_rule_wraps_to_previous_day():
    cfg = mk(heat_rules=[{"days": [6], "time": "20:00", "temp": 36}], eco_temp=28)
    assert S.evaluate(cfg, at(8), current_temp=30).setpoint == 36


def test_two_rules_pick_most_recent():
    cfg = mk(heat_rules=[
        {"days": [0, 1, 2, 3, 4, 5, 6], "time": "07:00", "temp": 38},
        {"days": [0, 1, 2, 3, 4, 5, 6], "time": "23:00", "temp": 30},
    ])
    assert S.evaluate(cfg, at(8), 30).setpoint == 38
    assert S.evaluate(cfg, at(23, 30), 35).setpoint == 30


def test_ready_by_preheat_window_active():
    cfg = mk(eco_temp=30, ready_by=[{"days": [0], "time": "10:00", "temp": 38}])
    # gap 4°, rate 4°/h -> 1h lead -> window starts 09:00
    d = S.evaluate(cfg, at(9), current_temp=34, heat_rate=4.0)
    assert d.setpoint == 38
    assert d.heater is True
    assert any(r["kind"] == "preheat" for r in d.reasons)


def test_ready_by_uses_base_rate_when_unspecified():
    # base rate 1°/h, gap 5° -> 5h lead -> window starts 05:00
    cfg = mk(eco_temp=30, heat_rate_c_per_h=1.0,
             ready_by=[{"days": [0], "time": "10:00", "temp": 35}])
    assert S.evaluate(cfg, at(4), current_temp=30).setpoint == 30   # before window
    assert S.evaluate(cfg, at(6), current_temp=30).setpoint == 35   # inside window


def test_ready_by_skipped_when_already_warm():
    cfg = mk(eco_temp=30, ready_by=[{"days": [0], "time": "10:00", "temp": 38}])
    assert S.evaluate(cfg, at(9), current_temp=38, heat_rate=4.0).setpoint == 30


def test_filter_window_on_without_heating():
    # at/above setpoint so heater is off; filter still on inside the window
    cfg = mk(eco_temp=25, filter_windows=[{"days": [0], "start": "08:00", "end": "12:00"}])
    d = S.evaluate(cfg, at(9), current_temp=30)
    assert d.heater is False
    assert d.filter is True


def test_filter_off_outside_window():
    cfg = mk(eco_temp=25, filter_windows=[{"days": [0], "start": "08:00", "end": "12:00"}])
    d = S.evaluate(cfg, at(14), current_temp=30)
    assert d.heater is False
    assert d.filter is False


def test_no_filter_management_when_unconfigured_and_not_heating():
    cfg = mk(eco_temp=25, filter_windows=[])
    d = S.evaluate(cfg, at(9), current_temp=30)  # above eco, no windows
    assert d.heater is False
    assert d.filter is None


def test_window_wraps_midnight():
    assert S._in_window(S.parse_hhmm("23:30"), "22:00", "06:00")
    assert S._in_window(S.parse_hhmm("02:00"), "22:00", "06:00")
    assert not S._in_window(S.parse_hhmm("12:00"), "22:00", "06:00")


def test_validate_rejects_bad_values():
    with pytest.raises(ValueError):
        S.validate_config({"heat_rules": [{"days": [9], "time": "07:00", "temp": 38}]})
    with pytest.raises(ValueError):
        S.validate_config({"heat_rules": [{"days": [0], "time": "07:00", "temp": 99}]})
    with pytest.raises(ValueError):
        S.validate_config({"heat_rate_c_per_h": 0})


def test_validate_fills_defaults_and_strips_unknown():
    cfg = S.validate_config({"enabled": True, "junk": 1})
    assert cfg["eco_temp"] == 30
    assert cfg["heat_rate_c_per_h"] == 1.0
    assert "junk" not in cfg
    assert "tempo" not in cfg  # tempo removed entirely


def test_config_roundtrip(tmp_path):
    f = tmp_path / "schedule.json"
    S.save_config(f, {"enabled": True, "heat_rules": [{"days": [0], "time": "07:00", "temp": 37}]})
    loaded = S.load_config(f)
    assert loaded["enabled"] is True
    assert loaded["heat_rules"][0]["temp"] == 37


def test_load_missing_returns_default():
    assert S.load_config("/nonexistent/schedule.json") == S.DEFAULT_CONFIG


def test_load_malformed_shape_returns_default(tmp_path):
    f = tmp_path / "schedule.json"
    f.write_text("[]")
    assert S.load_config(f) == S.DEFAULT_CONFIG


def test_estimate_heat_rate():
    pts = [
        {"t": 0, "cur": 30, "set": 38, "heat": True},
        {"t": 3600, "cur": 34, "set": 38, "heat": True},
        {"t": 7200, "cur": 38, "set": 38, "heat": True},
    ]
    assert S.estimate_heat_rate(pts) == pytest.approx(4.0, abs=0.5)


def test_estimate_heat_rate_default_without_signal():
    assert S.estimate_heat_rate([{"t": 0, "cur": 30, "heat": False}], default=1.0) == 1.0


# -- weather-aware heat-rate model --------------------------------------------
def _cooling_pts(k_loss, air, start_water, n=5, step_s=900):
    """heater-off samples that fall at k_loss·(water-air)."""
    pts, w = [], float(start_water)
    for i in range(n):
        pts.append({"t": i * step_s, "cur": round(w, 3), "heat": False, "air": air})
        w -= k_loss * (w - air) * (step_s / 3600.0)
    return pts


def _heating_pts(r_gross, k_loss, air, start_water, n=5, step_s=900, t0=100000):
    """heater-on samples that rise at r_gross - k_loss·(water-air)."""
    pts, w = [], float(start_water)
    for i in range(n):
        pts.append({"t": t0 + i * step_s, "cur": round(w, 3), "heat": True, "air": air})
        w += (r_gross - k_loss * (w - air)) * (step_s / 3600.0)
    return pts


def test_calibrate_and_predict():
    pts = _cooling_pts(0.05, 10, 30) + _heating_pts(1.7, 0.05, 10, 20)
    coeffs = S.calibrate_rates(pts)
    assert coeffs is not None
    r_gross, k_loss = coeffs
    assert r_gross == pytest.approx(1.7, abs=0.2)
    assert k_loss == pytest.approx(0.05, abs=0.02)
    # colder outside -> bigger gap -> slower predicted climb
    warm = S.predict_heat_rate(25, 15, r_gross, k_loss)
    cold = S.predict_heat_rate(25, 0, r_gross, k_loss)
    assert cold < warm


def test_calibrate_returns_none_without_air():
    pts = [{"t": i * 900, "cur": 20 + i, "heat": True} for i in range(6)]  # no air field
    assert S.calibrate_rates(pts) is None


def test_effective_rate_no_weather_is_measured():
    rate, ex = S.effective_heat_rate([], None, default=1.0)
    assert rate == 1.0 and ex["source"] == "measured"


def test_effective_rate_cold_derate_when_uncalibrated():
    cold_rate, cold = S.effective_heat_rate([], 5.0, default=1.0)   # 1 - 0.025*(15-5)=0.75
    warm_rate, warm = S.effective_heat_rate([], 20.0, default=1.0)  # >=ref -> factor 1
    assert cold["source"] == "weather-derate"
    assert cold_rate == pytest.approx(0.75, abs=0.001)
    assert warm_rate == pytest.approx(1.0, abs=0.001)
    assert cold_rate < warm_rate


def test_effective_rate_prefers_calibration():
    pts = _cooling_pts(0.05, 10, 30) + _heating_pts(1.7, 0.05, 10, 20)
    rate, ex = S.effective_heat_rate(pts, air=5.0, water=25.0)
    assert ex["source"] == "calibrated"
    assert ex["water"] == 25.0 and "k_loss" in ex
    assert rate == ex["effective"]


def test_estimate_ignores_single_degree_quantization_steps():
    # +1 °C over 0.3 h would read as 3.3 °C/h — must be rejected (needs >=2 °C rise)
    pts = [{"t": 0, "cur": 20, "heat": True}, {"t": 1080, "cur": 21, "heat": True}]
    assert S.estimate_heat_rate(pts, default=1.0) == 1.0


def test_effective_rate_capped_at_physical_ceiling():
    # an absurd measured/base rate is clamped to MAX_RATE (no spa heats this fast)
    rate, ex = S.effective_heat_rate([], None, default=10.0)
    assert rate == S.MAX_RATE
    assert ex["base"] == S.MAX_RATE


def test_predict_rate_capped():
    # tiny gap + ambient gain would over-predict; cap holds
    assert S.predict_heat_rate(20, 25, r_gross=1.7, k_loss=0.1) == S.MAX_RATE


def test_next_preheat_reports_start_and_lead():
    cfg = mk(ready_by=[{"days": [0], "time": "18:00", "temp": 36}])
    p = S.next_preheat(cfg, at(14), current_temp=22, rate=1.0)  # gap 14, capped lead 12h
    assert p["temp"] == 36 and p["time"] == "18:00"
    assert p["lead_h"] == 12.0 and p["start"] == "06:00"
    assert p["active"] is True


# -- eta_to_setpoint ----------------------------------------------------------
def test_eta_basic_same_day():
    eta = S.eta_to_setpoint(at(8), current_temp=30, target_temp=35, rate=1.0)
    assert eta["minutes"] == 300        # 5 °C at 1 °C/h
    assert eta["hours"] == 5.0
    assert eta["at"] == "13:00"         # 08:00 + 5 h, still Monday
    assert eta["label"] == "5 h"
    assert eta["over_cap"] is False


def test_eta_fractional_label():
    eta = S.eta_to_setpoint(at(8), current_temp=33, target_temp=35, rate=1.5)
    assert eta["minutes"] == 80         # 2 °C / 1.5 °C/h = 1h20
    assert eta["label"] == "1 h 20"


def test_eta_none_when_at_or_above_target():
    assert S.eta_to_setpoint(at(8), 35, 35, 1.0) is None
    assert S.eta_to_setpoint(at(8), 36, 35, 1.0) is None


def test_eta_none_on_missing_or_zero_inputs():
    assert S.eta_to_setpoint(at(8), None, 35, 1.0) is None
    assert S.eta_to_setpoint(at(8), 30, None, 1.0) is None
    assert S.eta_to_setpoint(at(8), 30, 35, 0) is None
    assert S.eta_to_setpoint(at(8), 30, 35, None) is None


def test_eta_over_cap_flag_for_cold_crawl():
    eta = S.eta_to_setpoint(at(8), current_temp=15, target_temp=35, rate=0.5)
    assert eta["over_cap"] is True      # 20 °C / 0.5 = 40 h > MAX_PREHEAT_HOURS


def test_eta_prefixes_weekday_when_next_day():
    eta = S.eta_to_setpoint(at(22), current_temp=30, target_temp=35, rate=1.0)
    assert eta["at"].startswith("Tue ")  # 22:00 + 5 h = 03:00 next day
