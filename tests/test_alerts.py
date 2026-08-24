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


def test_water_below_the_floor_fires_only_when_the_setpoint_is_higher():
    cold = {"current_temp": 28, "preset_temp": 37, "heater": False, "error_code": None}
    firing = evaluate(
        online=True, status=cold, last_ok_at=NOW, samples=[], config=AlertConfig(), now=NOW
    )
    assert WATER_LOW in firing

    # Same temperature, but that's what was asked for: nothing is wrong.
    resting = {"current_temp": 28, "preset_temp": 25, "heater": False, "error_code": None}
    firing = evaluate(
        online=True, status=resting, last_ok_at=NOW, samples=[], config=AlertConfig(), now=NOW
    )
    assert WATER_LOW not in firing


def test_water_floor_can_be_disabled():
    cold = {"current_temp": 12, "preset_temp": 37, "heater": False, "error_code": None}
    firing = evaluate(
        online=True, status=cold, last_ok_at=NOW, samples=[],
        config=AlertConfig(water_low_c=None), now=NOW,
    )
    assert WATER_LOW not in firing


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
    assert saved[UNREACHABLE]["notified_at"] == NOW
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
    assert "erreur disparue" in sender.sent[1]


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
