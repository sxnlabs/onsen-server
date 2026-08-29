"""Rules and state machine of the SMS watchdog — no clock, no spa, no OVH."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from intex_spa.alerts import (
    ERROR_CODE,
    FAILURE_ALARM_AFTER,
    RECOVERY_TICKS,
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
    watchdog = monitor(tmp_path, sender, online=True, status=cold, last_ok_at=NOW)
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


# -- a state file we didn't write ----------------------------------------------


async def test_an_episode_missing_notified_at_does_not_kill_the_tick(tmp_path):
    """The silent death this module exists to prevent, turned on itself: a
    truncated or hand-edited alerts.json used to raise KeyError on every tick,
    and `_loop` logged it as non-fatal forever. The watchdog was dead and the
    only trace was a log line nobody reads."""
    (tmp_path / "alerts.json").write_text(
        json.dumps({"episodes": {UNREACHABLE: {"since": NOW - 7200}}, "last_ok_at": NOW - 7200})
    )
    sender = FakeSender()
    watchdog = monitor(tmp_path, sender)

    await watchdog.tick(now=NOW)

    # Nothing was ever texted about this episode, so it still owes its SMS.
    assert len(sender.sent) == 1 and "injoignable" in sender.sent[0]


async def test_entries_that_are_not_episodes_are_dropped_not_carried(tmp_path):
    (tmp_path / "alerts.json").write_text(
        json.dumps(
            {
                "episodes": {
                    UNREACHABLE: {"since": NOW - 7200, "notified_at": NOW - 7000},
                    ERROR_CODE: {"since": "hier"},
                    WATER_LOW: "cold",
                    HEATING_STALLED: {"notified_at": NOW},
                }
            }
        )
    )
    watchdog = monitor(tmp_path, FakeSender())
    assert set(watchdog.snapshot()["episodes"]) == {UNREACHABLE}


async def test_a_junk_timestamp_does_not_become_an_episode_from_1970(tmp_path):
    """`isinstance(True, int)` is True, and float(True) is 1.0 — an epoch in
    January 1970, which would read as a 56-year outage."""
    (tmp_path / "alerts.json").write_text(
        json.dumps({"episodes": {UNREACHABLE: {"since": True, "notified_at": "oui"}}})
    )
    watchdog = monitor(tmp_path, FakeSender())
    assert watchdog.snapshot()["episodes"] == {}


async def test_a_repeated_tick_failure_stops_being_invisible(tmp_path):
    """`_loop` must keep the watchdog alive without hiding that it is broken:
    what it swallows has to show up on /api/alerts, which is what DEPLOY.md
    tells the operator to curl."""
    watchdog = monitor(tmp_path, FakeSender())
    assert watchdog.snapshot()["failing"] is None

    async def boom(*args, **kwargs):
        raise RuntimeError("history is on fire")

    watchdog.tick = boom
    watchdog.interval = 0
    task = asyncio.create_task(watchdog._loop())
    for _ in range(50):
        await asyncio.sleep(0)
        if (watchdog.snapshot()["failing"] or {}).get("count", 0) >= 2:
            break
    task.cancel()

    failing = watchdog.snapshot()["failing"]
    assert failing["count"] >= 2
    assert "history is on fire" in failing["error"]


async def _spin(watchdog, tick, until, limit=4000):
    """Run `_loop` with no sleep until `until()` holds, then stop it."""
    watchdog.tick = tick
    watchdog.interval = 0
    task = asyncio.create_task(watchdog._loop())
    for _ in range(limit):
        await asyncio.sleep(0)
        if until():
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_a_fault_that_comes_and_goes_still_reaches_the_alarm(tmp_path):
    """The bug this counter exists for is reached only while a condition fires,
    and three of the four rules come and go. Counting *consecutive* failures
    would cap the streak at one forever: the alarm would never sound and
    /api/alerts would read healthy every other minute."""
    watchdog = monitor(tmp_path, FakeSender())
    calls = {"n": 0}

    async def every_other(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] % 2:
            raise RuntimeError("history is on fire")
        return {}

    await _spin(
        watchdog,
        every_other,
        lambda: (watchdog.snapshot()["failing"] or {}).get("count", 0) >= FAILURE_ALARM_AFTER,
    )
    failing = watchdog.snapshot()["failing"]
    assert failing["count"] >= FAILURE_ALARM_AFTER
    assert "history is on fire" in failing["error"]


def test_a_streak_clears_only_after_a_sustained_clean_run(tmp_path):
    watchdog = monitor(tmp_path, FakeSender())
    watchdog._note_failure(RuntimeError("boom"))
    for _ in range(RECOVERY_TICKS - 1):
        watchdog._note_success()
        assert watchdog.snapshot()["failing"] is not None  # one good tick proves nothing

    watchdog._note_success()
    assert watchdog.snapshot()["failing"] is None

    # And the next failure dates from itself, not from the one already resolved:
    # DEPLOY.md tells the operator to read that age as the length of an outage.
    before = time.time()
    watchdog._note_failure(RuntimeError("encore"))
    failing = watchdog.snapshot()["failing"]
    assert failing["count"] == 1
    assert failing["since"] >= before
    assert failing["error"].endswith("encore")


async def test_the_loop_clears_its_own_streak_once_it_recovers(tmp_path):
    """Same contract, through `_loop` rather than by hand."""
    watchdog = monitor(tmp_path, FakeSender())
    calls = {"n": 0}

    async def once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {}

    await _spin(watchdog, once, lambda: calls["n"] > RECOVERY_TICKS)
    assert watchdog.snapshot()["failing"] is None


async def test_the_snapshot_carries_proof_of_life(tmp_path):
    """`failing: null` is also what a loop that never started looks like, and
    what one killed by a BaseException looks like. DEPLOY.md sends the operator
    to this endpoint to decide whether anything is watching."""
    watchdog = monitor(tmp_path, FakeSender())
    assert watchdog.snapshot()["alive"] is False
    assert watchdog.snapshot()["last_tick_at"] is None

    watchdog.interval = 0
    await watchdog.start()
    for _ in range(200):
        await asyncio.sleep(0)
        if watchdog.snapshot()["last_tick_at"] is not None:
            break
    assert watchdog.snapshot()["alive"] is True
    assert watchdog.snapshot()["last_tick_at"] is not None

    await watchdog.stop()
    assert watchdog.snapshot()["alive"] is False


def test_the_log_escalates_rather_than_repeating_one_traceback_a_minute(tmp_path, caplog):
    """Non-fatal is a claim with an expiry date. Once the streak holds, the line
    has to say the watchdog is not watching — and then shut up until it's worth
    saying again."""
    watchdog = monitor(tmp_path, FakeSender())
    with caplog.at_level(logging.CRITICAL, logger="intex_spa.alerts"):
        for _ in range(FAILURE_ALARM_AFTER):
            watchdog._note_failure(RuntimeError("history is on fire"))
        assert [r.getMessage() for r in caplog.records] == [
            f"alert tick has failed {FAILURE_ALARM_AFTER} times running "
            "(RuntimeError: history is on fire) — nothing is watching the spa"
        ]

        caplog.clear()
        for _ in range(60 - FAILURE_ALARM_AFTER):
            watchdog._note_failure(RuntimeError("history is on fire"))
        assert [r.getMessage() for r in caplog.records] == [
            "alert tick still failing after 60 attempts (RuntimeError: history is on fire)"
        ]


async def test_the_fault_code_survives_a_restart(tmp_path):
    """E90 texted, redeploy, spa now says E94. Without `code` reloaded off disk
    the new episode looks like the same fault, and the `notified_at` it carries
    swallows the worse diagnosis."""
    sender = FakeSender()
    config = AlertConfig(error_code_after=0)
    watchdog = monitor(
        tmp_path, sender, online=True, status=dict(OK, error_code="E90"),
        last_ok_at=NOW, config=config,
    )
    await watchdog.tick(now=NOW)
    assert len(sender.sent) == 1 and "E90" in sender.sent[0]

    restarted = monitor(
        tmp_path, sender, online=True, status=dict(OK, error_code="E94"),
        last_ok_at=NOW, config=config,
    )
    await restarted.tick(now=NOW + 60)
    assert len(sender.sent) == 2 and "E94" in sender.sent[1]


async def test_a_pending_resolution_survives_a_restart(tmp_path):
    """A resolution owed but not yet sent must still be owed after a redeploy."""
    sender = FakeSender()
    watchdog = monitor(tmp_path, sender)
    await watchdog.tick(now=NOW)

    sender.ok = False
    watchdog.supervisor.state["online"] = True
    watchdog.supervisor.last_ok_at = NOW + 60
    await watchdog.tick(now=NOW + 60)
    assert watchdog.snapshot()["episodes"][UNREACHABLE]["clearing"] is True

    restarted = monitor(tmp_path, sender, online=True, last_ok_at=NOW + 60)
    assert restarted.snapshot()["episodes"][UNREACHABLE]["clearing"] is True


def test_the_witness_is_a_timestamp_or_nothing(tmp_path):
    """`float(True)` is 1.0 — January 1970 — and the witness dates the outage:
    it would text "spa injoignable depuis 499999h59"."""
    (tmp_path / "alerts.json").write_text(
        json.dumps({"episodes": {}, "last_ok_at": NOW - 7200})
    )
    assert monitor(tmp_path, FakeSender(), last_ok_at=None)._last_ok_at([]) == NOW - 7200

    (tmp_path / "alerts.json").write_text(json.dumps({"episodes": {}, "last_ok_at": True}))
    watchdog = monitor(tmp_path, FakeSender(), last_ok_at=None)
    assert watchdog._last_ok_at([]) == watchdog.started_at
