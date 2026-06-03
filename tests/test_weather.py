"""Weather client: parsing, interpolation, caching, fail-soft — all offline."""

import json

from intex_spa.weather import WeatherClient

T0 = 1_700_000_000  # arbitrary epoch, aligned below to whole hours
T0 -= T0 % 3600


def _payload(temps, feels=None, wind=None, code=None, sunrise=None, sunset=None, start=T0):
    """Build an Open-Meteo-shaped hourly payload (unixtime)."""
    n = len(temps)
    times = [start + i * 3600 for i in range(n)]
    return {
        "hourly": {
            "time": times,
            "temperature_2m": temps,
            "apparent_temperature": feels if feels is not None else temps,
            "wind_speed_10m": wind if wind is not None else [0.0] * n,
            "weather_code": code if code is not None else [0] * n,
        },
        "daily": {
            "sunrise": sunrise if sunrise is not None else [start + 6 * 3600],
            "sunset": sunset if sunset is not None else [start + 20 * 3600],
        },
    }


def _client(monkeypatch_payload):
    c = WeatherClient(cache_path=None)
    c._fetch_blocking = lambda: monkeypatch_payload  # type: ignore[method-assign]
    return c


async def test_refresh_parses_and_interpolates():
    c = _client(_payload([10.0, 12.0, 14.0, 16.0]))
    assert await c.refresh(force=True) is True
    # exact hour
    assert c.air_at(T0) == 10.0
    assert c.air_at(T0 + 3600) == 12.0
    # halfway between hour 0 and 1 -> 11.0
    assert c.air_at(T0 + 1800) == 11.0
    # before first / after last -> clamp
    assert c.air_at(T0 - 99999) == 10.0
    assert c.air_at(T0 + 99 * 3600) == 16.0


async def test_air_window_mean_and_low():
    c = _client(_payload([10.0, 20.0, 30.0]))  # hours 0,1,2
    await c.refresh(force=True)
    # window covering all three hours: mean of endpoints(10,30) + interior(20) = 20
    assert c.air_window(T0, T0 + 2 * 3600) == 20.0
    assert c.low_ahead(hours=3, now=T0) == 10.0


async def test_ttl_skips_redundant_fetch():
    c = _client(_payload([5.0, 6.0]))
    assert await c.refresh(force=True) is True
    # fresh cache, no force -> no network fetch
    assert await c.refresh(now=c._fetched_at + 10) is False
    # past the TTL -> fetches again
    assert await c.refresh(now=c._fetched_at + c.ttl + 1) is True


async def test_fetch_failure_keeps_previous():
    c = _client(_payload([7.0, 8.0]))
    assert await c.refresh(force=True) is True
    assert c.air_at(T0) == 7.0

    def boom():
        raise OSError("network down")

    c._fetch_blocking = boom  # type: ignore[method-assign]
    assert await c.refresh(force=True) is False
    assert c.air_at(T0) == 7.0  # still serving the last good forecast


async def test_cache_persists_across_instances(tmp_path):
    path = tmp_path / "weather.json"
    c1 = WeatherClient(cache_path=path)
    c1._fetch_blocking = lambda: _payload([1.0, 2.0, 3.0])  # type: ignore[method-assign]
    assert await c1.refresh(force=True) is True
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["hours"][0]["air"] == 1.0
    # a fresh client reads the cache without any fetch
    c2 = WeatherClient(cache_path=path)
    assert c2.air_at(T0) == 1.0


async def test_snapshot_shape():
    c = _client(_payload(
        [12.0, 9.0, 6.0],
        feels=[11.0, 7.0, 4.0],
        wind=[10.0, 20.0, 30.0],
        code=[0, 3, 61],
        sunrise=[T0 + 6 * 3600],
        sunset=[T0 + 20 * 3600],
    ))
    await c.refresh(force=True)
    s = c.snapshot(now=T0)
    assert s["air"] == 12.0 and s["feels"] == 11.0 and s["wind"] == 10.0
    assert s["weather_code"] == 0 and s["condition"] == "clear"
    assert s["sunrise"] == T0 + 6 * 3600 and s["sunset"] == T0 + 20 * 3600
    assert s["low_12h"] == 6.0
    assert s["hours"] == 3 and s["source"] == "open-meteo"


async def test_snapshot_classifies_cloudy_codes():
    c = _client(_payload([12.0, 9.0, 6.0], code=[0, 3, 61]))
    await c.refresh(force=True)
    s = c.snapshot(now=T0 + 3600)
    assert s["weather_code"] == 3
    assert s["condition"] == "cloudy"
