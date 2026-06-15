"""End-to-end client tests against the fake spa (no real hardware)."""

import pytest

from fake_spa import FakeSpa
from intex_spa.client import IntexSpaClient, SpaUnreachable


async def _client_for(spa: FakeSpa) -> IntexSpaClient:
    host, port = await spa.start()
    return IntexSpaClient(host, port=port, timeout=2.0, retries=1)


async def test_status_roundtrip():
    spa = FakeSpa()
    c = await _client_for(spa)
    try:
        st = await c.status()
        assert st["power"] is True
        assert st["current_temp"] == 19
        assert st["preset_temp"] == 37
        assert spa.intents == ["status"]
    finally:
        await c.close()
        await spa.stop()


async def test_oversized_frame_does_not_crash(monkeypatch):
    """A peer sending an oversized/garbled frame (asyncio readline limit overrun
    raises ValueError) must surface as SpaUnreachable, not an uncaught crash."""
    spa = FakeSpa()
    c = await _client_for(spa)

    async def boom(self, *args, **kwargs):
        raise ValueError("Separator is not found, and chunk exceed the limit")

    monkeypatch.setattr("asyncio.StreamReader.readline", boom)
    try:
        with pytest.raises(SpaUnreachable):
            await c.status()
    finally:
        await c.close()
        await spa.stop()


async def test_set_toggles_when_state_differs():
    spa = FakeSpa()  # bubbles starts False
    c = await _client_for(spa)
    try:
        st = await c.set("bubbles", True)
        assert st["bubbles"] is True
        assert spa.intents == ["status", "bubbles"]  # read, then one toggle
    finally:
        await c.close()
        await spa.stop()


async def test_set_is_idempotent_no_spurious_toggle():
    spa = FakeSpa()  # power already True
    c = await _client_for(spa)
    try:
        st = await c.set("power", True)
        assert st["power"] is True
        # only a status read — a toggle here would have turned the spa OFF
        assert spa.intents == ["status"]
    finally:
        await c.close()
        await spa.stop()


async def test_set_preset_absolute_then_idempotent():
    spa = FakeSpa()
    c = await _client_for(spa)
    try:
        st = await c.set_preset(40)
        assert st["preset_temp"] == 40
        assert spa.intents == ["status", "preset_temp"]
        st = await c.set_preset(40)  # already 40 -> no command
        assert st["preset_temp"] == 40
        assert spa.intents == ["status", "preset_temp", "status"]
    finally:
        await c.close()
        await spa.stop()


async def test_set_preset_out_of_range_rejected():
    spa = FakeSpa()
    c = await _client_for(spa)
    try:
        with pytest.raises(ValueError):
            await c.set_preset(50)
        with pytest.raises(ValueError):
            await c.set_preset(10)
    finally:
        await c.close()
        await spa.stop()


async def test_set_rejects_non_toggle_field():
    spa = FakeSpa()
    c = await _client_for(spa)
    try:
        with pytest.raises(ValueError):
            await c.set("preset_temp", True)
    finally:
        await c.close()
        await spa.stop()


async def test_heater_on_forces_filter_on_first():
    spa = FakeSpa()  # filter False, heater False
    c = await _client_for(spa)
    try:
        st = await c.set("heater", True)
        # circulation must come up BEFORE heat: read, toggle filter, toggle heater
        assert spa.intents == ["status", "filter", "heater"]
        assert st["filter"] is True
        assert st["heater"] is True
    finally:
        await c.close()
        await spa.stop()


async def test_heater_on_skips_filter_when_already_circulating():
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "filter": True})
    c = await _client_for(spa)
    try:
        st = await c.set("heater", True)
        assert spa.intents == ["status", "heater"]  # no spurious filter toggle
        assert st["heater"] is True
    finally:
        await c.close()
        await spa.stop()


async def test_heater_on_rejected_without_valid_water_temperature():
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "current_temp": 181})
    c = await _client_for(spa)
    try:
        with pytest.raises(ValueError):
            await c.set("heater", True)
        assert spa.intents == ["status"]
        assert spa.state["heater"] is False
        assert spa.state["filter"] is False
    finally:
        await c.close()
        await spa.stop()


async def test_filter_off_cuts_heater_first():
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "filter": True, "heater": True})
    c = await _client_for(spa)
    try:
        st = await c.set("filter", False)
        # never leave the heater running dry: cut heat, then stop circulation
        assert spa.intents == ["status", "heater", "filter"]
        assert st["heater"] is False
        assert st["filter"] is False
    finally:
        await c.close()
        await spa.stop()


async def test_filter_off_leaves_heater_alone_when_already_off():
    spa = FakeSpa({**FakeSpa.DEFAULT_STATE, "filter": True})  # heater False
    c = await _client_for(spa)
    try:
        st = await c.set("filter", False)
        assert spa.intents == ["status", "filter"]
        assert st["filter"] is False
    finally:
        await c.close()
        await spa.stop()


async def test_toggle_not_resent_when_ack_is_lost():
    # The module applies the filter toggle but reports result:'timeout' (the ack
    # is lost on a flaky link). The client must NOT blindly re-send the toggle —
    # that would flip the relay straight back off. It must re-read and stop.
    spa = FakeSpa()  # filter starts False
    c = await _client_for(spa)
    try:
        spa.timeout_on = {"filter": 1}  # the filter toggle lands, ack comes back 'timeout'
        st = await c.set("filter", True)
        assert st["filter"] is True              # ended up where we asked
        assert spa.intents.count("filter") == 1  # actuated exactly once, no cycle
        assert spa.intents == ["status", "filter", "status"]  # send, then verify
    finally:
        await c.close()
        await spa.stop()


async def test_reconnects_after_broken_socket():
    spa = FakeSpa()
    c = await _client_for(spa)
    try:
        await c.status()
        await c._disconnect()  # simulate a dropped/idle-killed connection
        st = await c.status()  # must transparently reconnect
        assert st["power"] is True
    finally:
        await c.close()
        await spa.stop()


async def test_unreachable_raises():
    # nothing listening on port 1 -> connection refused, fast
    c = IntexSpaClient("127.0.0.1", port=1, timeout=0.5, retries=1)
    with pytest.raises(SpaUnreachable):
        await c.status()
