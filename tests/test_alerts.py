"""Rules and state machine of the SMS watchdog — no clock, no spa, no OVH."""

from __future__ import annotations

import json

from intex_spa.alerts import (
    ERROR_CODE,
    HEATING_STALLED,
    UNREACHABLE,
    WATER_LOW,
    AlertConfig,
    AlertMonitor,
    resolution_message,
    evaluate,
)
from intex_spa.history import TempHistory

NOW = 1_800_000_000.0
OK = {"current_temp": 36, "preset_temp": 37, "heater": True, "error_code": None}


class FakeSender:
    """Records what would have been texted; can fail on demand."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send(self, message: str) -> bool:
        if not self.ok:
            return False
        self.sent.append(message)
        return True


class FakeSupervisor:
    def __init__(self, *, online: bool, status: dict | None, last_ok_at: float | None) -> None:
        self.state = {"online": online, "status": status, "error": None, "updated_at": NOW}
        self.last_ok_at = last_ok_at
        self.history = TempHistory(path=None)


def heating_samples(*, hours: float, rise: int, heat: bool = True, start_temp: int = 30) -> list[dict]:
    """One sample a minute across `hours`, climbing `rise` degrees in total."""
    points = []
    steps = int(hours * 60)
    for i in range(steps + 1):
        points.append(
            {
                "t": NOW - hours * 3600 + i * 60,
                "cur": start_temp + (rise * i) // max(steps, 1),
                "set": 38,
                "heat": heat,
            }
        )
    return points


# -- evaluate(): the rules -----------------------------------------------------


def test_offline_under_the_delay_says_nothing():
    firing = evaluate(
        online=False, status=OK, last_ok_at=NOW - 1800, samples=[],
        config=AlertConfig(), now=NOW,
    )
    assert firing == {}


def test_offline_past_the_delay_reports_the_outage():
    firing = evaluate(
        online=False, status=OK, last_ok_at=NOW - 3700, samples=[],
        config=AlertConfig(), now=NOW,
    )
    assert set(firing) == {UNREACHABLE}
    assert "injoignable depuis 1h01" in firing[UNREACHABLE]


def test_offline_suppresses_the_spa_side_rules():
    """A two-day-old frame must not produce "water at 20C" alerts."""
    cold = {"current_temp": 20, "preset_temp": 37, "heater": True, "error_code": "E90"}
    firing = evaluate(
        online=False, status=cold, last_ok_at=NOW - 3700, samples=[],
        config=AlertConfig(), now=NOW,
    )
    assert set(firing) == {UNREACHABLE}


def test_never_reached_the_spa_stays_quiet():
    firing = evaluate(
        online=False, status=None, last_ok_at=None, samples=[],
        config=AlertConfig(), now=NOW,
    )
    assert firing == {}


def test_error_code_is_reported_with_its_code():
    status = dict(OK, error_code="E90")
    firing = evaluate(
        online=True, status=status, last_ok_at=NOW, samples=[],
        config=AlertConfig(), now=NOW,
    )
    assert set(firing) == {ERROR_CODE}
    assert "E90" in firing[ERROR_CODE]


ARMED_FLOOR = AlertConfig(water_low_c=30.0)  # the floor is opt-in; arm it to test it


def test_water_below_the_floor_fires_only_when_the_setpoint_is_higher():
    cold = {"current_temp": 28, "preset_temp": 37, "heater": False, "error_code": None}
    firing = evaluate(
        online=True, status=cold, last_ok_at=NOW, samples=[], config=ARMED_FLOOR, now=NOW
    )
    assert WATER_LOW in firing

    # Same temperature, but that's what was asked for: nothing is wrong.
    resting = {"current_temp": 28, "preset_temp": 25, "heater": False, "error_code": None}
    firing = evaluate(
        online=True, status=resting, last_ok_at=NOW, samples=[], config=ARMED_FLOOR, now=NOW
    )
    assert WATER_LOW not in firing


def test_water_floor_is_off_by_default():
    """A spa climbing from 29C to 37C is a cold start, not an incident: only the
    three fault rules are allowed to spend an SMS."""
    cold = {"current_temp": 29, "preset_temp": 37, "heater": True, "error_code": None}
    firing = evaluate(
        online=True, status=cold, last_ok_at=NOW, samples=[], config=AlertConfig(), now=NOW
    )
    assert firing == {}


def test_two_hours_of_heat_without_a_rise_is_a_stall():
    firing = evaluate(
        online=True, status=OK, last_ok_at=NOW,
        samples=heating_samples(hours=2, rise=0),
        config=AlertConfig(), now=NOW,
    )
    assert HEATING_STALLED in firing


def test_heat_that_climbs_is_not_a_stall():
    firing = evaluate(
        online=True, status=OK, last_ok_at=NOW,
        samples=heating_samples(hours=2, rise=3),
        config=AlertConfig(), now=NOW,
    )
    assert HEATING_STALLED not in firing


def test_heater_off_is_not_a_stall():
    firing = evaluate(
        online=True, status=OK, last_ok_at=NOW,
        samples=heating_samples(hours=2, rise=0, heat=False),
        config=AlertConfig(), now=NOW,
    )
    assert HEATING_STALLED not in firing


def test_a_short_window_after_a_restart_is_not_a_stall():
    """The regression that would text on every recovery: few samples, no span."""
    firing = evaluate(
        online=True, status=OK, last_ok_at=NOW,
        samples=heating_samples(hours=0.2, rise=0),
        config=AlertConfig(), now=NOW,
    )
    assert HEATING_STALLED not in firing


def test_already_at_setpoint_is_not_a_stall():
    samples = heating_samples(hours=2, rise=0, start_temp=38)
    for point in samples:
        point["set"] = 37
    firing = evaluate(
        online=True, status=OK, last_ok_at=NOW, samples=samples,
        config=AlertConfig(), now=NOW,
    )
    assert HEATING_STALLED not in firing


# -- AlertMonitor: debounce, one SMS per episode, resolution -------------------


def monitor(tmp_path, sender, *, online=False, status=OK, last_ok_at=NOW - 3700, config=None):
    return AlertMonitor(
        FakeSupervisor(online=online, status=status, last_ok_at=last_ok_at),
        sender,
        config=config or AlertConfig(),
        state_path=tmp_path / "alerts.json",
        interval=60.0,
    )


async def test_texts_once_then_stays_quiet(tmp_path):
    sender = FakeSender()
    watchdog = monitor(tmp_path, sender)

    await watchdog.tick(now=NOW)
    assert len(sender.sent) == 1

    # The 46h outage is worth one text, not one per minute.
    for offset in (60, 600, 46 * 3600):
        await watchdog.tick(now=NOW + offset)
    assert len(sender.sent) == 1


async def test_texts_again_when_it_clears(tmp_path):
    sender = FakeSender()
    watchdog = monitor(tmp_path, sender)
    await watchdog.tick(now=NOW)

    watchdog.supervisor.state["online"] = True
    watchdog.supervisor.last_ok_at = NOW + 60
    await watchdog.tick(now=NOW + 60)

    assert len(sender.sent) == 2
    assert "joignable" in sender.sent[1]


async def test_a_failed_send_is_retried_next_tick(tmp_path):
    sender = FakeSender(ok=False)
    watchdog = monitor(tmp_path, sender)
    await watchdog.tick(now=NOW)
    assert sender.sent == []

    sender.ok = True
    await watchdog.tick(now=NOW + 60)
    assert len(sender.sent) == 1


async def test_a_restart_mid_outage_does_not_text_again(tmp_path):
    sender = FakeSender()
    await monitor(tmp_path, sender).tick(now=NOW)
    assert len(sender.sent) == 1

    # New process, same state file, spa still down.
    restarted = monitor(tmp_path, sender, last_ok_at=None)
    restarted.supervisor.history.record(36, 37, True, ts=NOW - 3700)
    await restarted.tick(now=NOW + 120)
    assert len(sender.sent) == 1


async def test_state_file_records_the_episode(tmp_path):
    sender = FakeSender()
    watchdog = monitor(tmp_path, sender)
    await watchdog.tick(now=NOW)

    saved = json.loads((tmp_path / "alerts.json").read_text())
    assert saved["episodes"][UNREACHABLE]["notified_at"] == NOW
    assert watchdog.snapshot()["episodes"][UNREACHABLE]["notified"] is True


# -- what the spa can't tell us while it's offline -----------------------------


async def test_offline_does_not_resolve_a_spa_side_episode(tmp_path):
    """An E90 followed by a connectivity loss must not text "erreur disparue"."""
    sender = FakeSender()
    faulty = dict(OK, error_code="E90")
    watchdog = monitor(
        tmp_path, sender, online=True, status=faulty, last_ok_at=NOW,
        config=AlertConfig(error_code_after=0),
    )
    await watchdog.tick(now=NOW)
    assert len(sender.sent) == 1 and "E90" in sender.sent[0]

    # Spa drops off the network, still inside the unreachable delay.
    watchdog.supervisor.state["online"] = False
    watchdog.supervisor.last_ok_at = NOW
    await watchdog.tick(now=NOW + 60)
    assert len(sender.sent) == 1  # nothing observed, nothing announced

    # Back online, error gone: now we've actually seen it clear.
    watchdog.supervisor.state.update(online=True, status=dict(OK))
    watchdog.supervisor.last_ok_at = NOW + 120
    await watchdog.tick(now=NOW + 120)
    assert len(sender.sent) == 2
    assert "plus d'erreur" in sender.sent[1]


async def test_resolution_sms_is_retried_until_it_lands(tmp_path):
    sender = FakeSender()
    watchdog = monitor(tmp_path, sender)
    await watchdog.tick(now=NOW)
    assert len(sender.sent) == 1

    sender.ok = False
    watchdog.supervisor.state["online"] = True
    watchdog.supervisor.last_ok_at = NOW + 60
    await watchdog.tick(now=NOW + 60)
    assert len(sender.sent) == 1
    assert watchdog.snapshot()["episodes"][UNREACHABLE]["clearing"] is True

    sender.ok = True
    await watchdog.tick(now=NOW + 120)
    assert len(sender.sent) == 2
    assert watchdog.snapshot()["episodes"] == {}


async def test_a_condition_that_comes_back_cancels_its_pending_resolution(tmp_path):
    sender = FakeSender(ok=False)
    watchdog = monitor(tmp_path, sender)
    watchdog.sender.ok = True
    await watchdog.tick(now=NOW)

    sender.ok = False  # resolution can't go out
    watchdog.supervisor.state["online"] = True
    watchdog.supervisor.last_ok_at = NOW + 60
    await watchdog.tick(now=NOW + 60)

    # Down again, and silent long enough to re-fire, before we could say it was
    # back. From the owner's side the incident never closed: no second alert,
    # and the stale resolution must not go out either.
    sender.ok = True
    watchdog.supervisor.state["online"] = False
    await watchdog.tick(now=NOW + 5000)
    assert len(sender.sent) == 1
    assert watchdog.snapshot()["episodes"][UNREACHABLE]["clearing"] is False


def test_error_text_omits_the_water_when_the_frame_has_none():
    """decode_status() sets current_temp=None exactly when it sets error_code."""
    faulty = {"current_temp": None, "preset_temp": 37, "heater": True, "error_code": "E90"}
    firing = evaluate(
        online=True, status=faulty, last_ok_at=NOW, samples=[], config=AlertConfig(), now=NOW
    )
    assert "None" not in firing[ERROR_CODE]
    assert "E90" in firing[ERROR_CODE]


def test_a_gap_in_the_samples_is_not_a_stall():
    """Two heater-on points either side of a polling outage span the window but
    say nothing about the hours in between."""
    samples = [
        {"t": NOW - 7200, "cur": 30, "set": 38, "heat": True},
        {"t": NOW - 60, "cur": 30, "set": 38, "heat": True},
    ]
    firing = evaluate(
        online=True, status=OK, last_ok_at=NOW, samples=samples, config=AlertConfig(), now=NOW
    )
    assert HEATING_STALLED not in firing


async def test_history_lookback_covers_a_long_unreachable_delay(tmp_path):
    """A restart 5h into an outage, with a 6h threshold: the last sample is still
    in TempHistory and must be found, or the outage clock restarts from boot."""
    sender = FakeSender()
    watchdog = monitor(
        tmp_path, sender, last_ok_at=None, config=AlertConfig(unreachable_after=6 * 3600)
    )
    watchdog.supervisor.history.record(36, 37, True, ts=NOW - 7 * 3600)
    await watchdog.tick(now=NOW)
    assert len(sender.sent) == 1


def test_the_live_heater_state_outranks_the_window():
    """record() can drop a heater-off point (same temp, <60s), leaving the window
    all-heat after the heater actually stopped."""
    off = dict(OK, heater=False)
    firing = evaluate(
        online=True, status=off, last_ok_at=NOW,
        samples=heating_samples(hours=2, rise=0),
        config=AlertConfig(), now=NOW,
    )
    assert HEATING_STALLED not in firing


async def test_no_resolution_while_the_spa_is_offline_again(tmp_path):
    """Reconnect, failed resolution, then a second outage still under the
    threshold: "de nouveau joignable" must not go out on top of a dead link."""
    sender = FakeSender()
    watchdog = monitor(tmp_path, sender)
    await watchdog.tick(now=NOW)

    sender.ok = False
    watchdog.supervisor.state["online"] = True
    watchdog.supervisor.last_ok_at = NOW + 60
    await watchdog.tick(now=NOW + 60)
    assert len(sender.sent) == 1

    sender.ok = True
    watchdog.supervisor.state["online"] = False  # down again, under the delay
    await watchdog.tick(now=NOW + 300)
    assert len(sender.sent) == 1
    assert watchdog.snapshot()["episodes"][UNREACHABLE]["clearing"] is True


async def test_an_error_frame_still_dates_the_outage_after_a_restart(tmp_path):
    """A spa answering E90 replies successfully but writes no history point, so
    the temp series alone would date the outage hours too early."""
    sender = FakeSender()
    faulty = {"current_temp": None, "preset_temp": 37, "heater": True, "error_code": "E90"}
    config = AlertConfig(error_code_after=10_000)  # keep the E90 alert out of the way
    watchdog = monitor(tmp_path, sender, online=True, status=faulty, last_ok_at=NOW, config=config)
    watchdog.supervisor.history.record(36, 37, True, ts=NOW - 6 * 3600)  # last real reading
    await watchdog.tick(now=NOW)
    assert sender.sent == []

    # Restart: last_ok_at is gone, the newest sample is six hours old.
    restarted = monitor(tmp_path, sender, online=False, status=faulty, last_ok_at=None, config=config)
    restarted.supervisor.history.record(36, 37, True, ts=NOW - 6 * 3600)
    await restarted.tick(now=NOW + 600)
    assert sender.sent == []  # the link failed ten minutes ago, not six hours ago

    await restarted.tick(now=NOW + 3700)
    assert len(sender.sent) == 1


async def test_an_interrupted_debounce_starts_over(tmp_path):
    """Water low for 5 of the 15 required minutes, then the link dies for hours:
    when it comes back the condition has not persisted — the clock restarts."""
    sender = FakeSender()
    cold = {"current_temp": 28, "preset_temp": 37, "heater": True, "error_code": None}
    watchdog = monitor(
        tmp_path, sender, online=True, status=cold, last_ok_at=NOW, config=ARMED_FLOOR
    )
    await watchdog.tick(now=NOW)
    assert watchdog.snapshot()["episodes"][WATER_LOW]["notified"] is False

    watchdog.supervisor.state["online"] = False
    await watchdog.tick(now=NOW + 300)
    assert WATER_LOW not in watchdog.snapshot()["episodes"]

    # Six hours later the spa is back, still cold: a fresh 15-minute clock.
    watchdog.supervisor.state["online"] = True
    watchdog.supervisor.last_ok_at = NOW + 6 * 3600
    await watchdog.tick(now=NOW + 6 * 3600)
    assert sender.sent == []
    await watchdog.tick(now=NOW + 6 * 3600 + 900)
    assert len(sender.sent) == 1 and "seuil" in sender.sent[0]


async def test_a_different_fault_is_a_different_alert(tmp_path):
    """E90 straight to E94 must not hide behind the already-notified episode."""
    sender = FakeSender()
    status = dict(OK, error_code="E90")
    watchdog = monitor(
        tmp_path, sender, online=True, status=status, last_ok_at=NOW,
        config=AlertConfig(error_code_after=0),
    )
    await watchdog.tick(now=NOW)
    assert len(sender.sent) == 1 and "E90" in sender.sent[0]

    await watchdog.tick(now=NOW + 60)
    assert len(sender.sent) == 1  # same fault, still one text

    watchdog.supervisor.state["status"] = dict(OK, error_code="E94")
    await watchdog.tick(now=NOW + 120)
    assert len(sender.sent) == 2 and "E94" in sender.sent[1]


def test_a_setpoint_lowered_mid_window_is_not_a_stall():
    """"eau 30C, consigne 30C" is an arrival, not a fault."""
    arrived = {"current_temp": 30, "preset_temp": 30, "heater": True, "error_code": None}
    firing = evaluate(
        online=True, status=arrived, last_ok_at=NOW,
        samples=heating_samples(hours=2, rise=0), config=AlertConfig(), now=NOW,
    )
    assert HEATING_STALLED not in firing


def test_an_error_frame_never_produces_a_stall_alert():
    """An online error frame keeps heater=True but has no reading: judging the
    pre-fault window would text a diagnosis containing "eau NoneC"."""
    faulty = {"current_temp": None, "preset_temp": 38, "heater": True, "error_code": "E90"}
    firing = evaluate(
        online=True, status=faulty, last_ok_at=NOW,
        samples=heating_samples(hours=2, rise=0), config=AlertConfig(), now=NOW,
    )
    assert HEATING_STALLED not in firing
    assert set(firing) == {ERROR_CODE}


def test_resolution_wording_claims_only_what_was_observed():
    """A stall episode also clears when the heater is switched off, and a
    water-low one when the setpoint is lowered under still-cold water."""
    cooled = {"current_temp": 28, "preset_temp": 27, "heater": False, "error_code": None}
    assert "repart" not in resolution_message(HEATING_STALLED, cooled)
    assert "au-dessus" not in resolution_message(WATER_LOW, cooled)
    assert "28C" in resolution_message(WATER_LOW, cooled)


async def test_disabling_a_rule_drops_its_episode_instead_of_clearing_it(tmp_path):
    """Turning the water floor off is not an observation. The owner who silenced
    it because of the cost must not get one last "alerte eau basse levee"."""
    sender = FakeSender()
    cold = {"current_temp": 28, "preset_temp": 37, "heater": True, "error_code": None}
    watchdog = monitor(
        tmp_path, sender, online=True, status=cold, last_ok_at=NOW,
        config=AlertConfig(water_low_c=30.0, water_low_after=0),
    )
    await watchdog.tick(now=NOW)
    assert len(sender.sent) == 1 and "seuil" in sender.sent[0]

    assert WATER_LOW in json.loads((tmp_path / "alerts.json").read_text())["episodes"]

    # Same state file, floor now off.
    restarted = monitor(tmp_path, sender, online=True, status=cold, last_ok_at=NOW)
    assert restarted.snapshot()["episodes"] == {}
    await restarted.tick(now=NOW + 60)
    assert len(sender.sent) == 1
    # And it's gone from disk, not just from memory: an episode that outlived the
    # purge would be reloaded the day the floor is armed again — texting a
    # resolution nobody asked for, then swallowing the first real freeze alert.
    assert json.loads((tmp_path / "alerts.json").read_text())["episodes"] == {}

    rearmed = monitor(
        tmp_path, sender, online=True, status=cold, last_ok_at=NOW, config=ARMED_FLOOR
    )
    await rearmed.tick(now=NOW + 120)
    assert len(sender.sent) == 1  # a fresh debounce, not a stale episode
    assert rearmed.snapshot()["episodes"][WATER_LOW]["notified"] is False


def test_disabled_keys_covers_every_rule_a_config_can_switch_off():
    """Both off switches the env exposes: an unset floor, and a zero-hour stall
    window (`ONSEN_ALERT_HEATING_STALL_HOURS=0`)."""
    assert AlertConfig().disabled_keys() == {WATER_LOW}
    assert AlertConfig(water_low_c=30.0).disabled_keys() == set()
    assert AlertConfig(heating_stall_hours=0).disabled_keys() == {WATER_LOW, HEATING_STALLED}


def _alert_config(monkeypatch):
    """Resolve AlertConfig the way make_app() does, off a given environment."""
    from web import main as web_main

    def configured(**env):
        monkeypatch.setattr(web_main, "alerting_env", lambda: env)
        return web_main._configured_alerting()[1]

    return configured


def test_the_env_arms_the_water_floor_only_when_it_names_a_temperature(monkeypatch):
    """Unset means off, so a deployment that never set the variable stops texting
    cold starts on its own. One that *does* still name 30 keeps the old noise —
    that line has to be deleted by hand (DEPLOY.md says so)."""
    config = _alert_config(monkeypatch)

    assert config().water_low_c is None
    assert config(ONSEN_ALERT_WATER_LOW_C="").water_low_c is None
    assert config(ONSEN_ALERT_WATER_LOW_C="OFF").water_low_c is None
    assert config(ONSEN_ALERT_WATER_LOW_C="0").water_low_c is None
    assert config(ONSEN_ALERT_WATER_LOW_C=" 5 ").water_low_c == 5.0
    assert config(ONSEN_ALERT_WATER_LOW_C="30").water_low_c == 30.0  # still armed
    # The three fault rules keep their thresholds.
    assert config().unreachable_after == 3600.0
    assert config().heating_stall_hours == 2.0


def test_a_bad_alerting_value_never_takes_the_spa_down(monkeypatch):
    """`make_app()` builds the supervisor too. A typo in an optional threshold
    that raised out of the factory would leave the spa unmanaged *and* mute —
    and "off" is a word the docs teach for the floor, so the stall window has to
    survive it as well."""
    config = _alert_config(monkeypatch)

    assert config(ONSEN_ALERT_HEATING_STALL_HOURS="off").heating_stall_hours == 0
    assert config(ONSEN_ALERT_HEATING_STALL_HOURS="deux").heating_stall_hours == 2.0
    assert config(ONSEN_ALERT_WATER_LOW_C="froid").water_low_c is None
    assert config(ONSEN_ALERT_UNREACHABLE_AFTER="une heure").unreachable_after == 3600.0
    # An off switch for the outage alarm is not on offer: that's the 46h incident.
    assert config(ONSEN_ALERT_UNREACHABLE_AFTER="off").unreachable_after == 3600.0
    assert config(ONSEN_ALERT_UNREACHABLE_AFTER="60").unreachable_after == 60.0


def test_a_zero_hour_stall_window_disables_the_stall_rule():
    """What `ONSEN_ALERT_HEATING_STALL_HOURS=0` buys, end to end."""
    config = AlertConfig(heating_stall_hours=0)
    firing = evaluate(
        online=True, status=OK, last_ok_at=NOW,
        samples=heating_samples(hours=2, rise=0), config=config, now=NOW,
    )
    assert firing == {}
    assert config.disabled_keys() == {WATER_LOW, HEATING_STALLED}
