"""Unit tests for TempHistory: throttle, retention, persistence."""

from intex_spa.history import TempHistory


def test_records_first_sample():
    h = TempHistory(path=None, min_interval=60)
    assert h.record(19, 37, False, ts=1000) is not None
    assert h.recent(hours=24, now=1000) == [{"t": 1000.0, "cur": 19, "set": 37, "heat": False}]


def test_throttle_skips_unchanged_within_interval():
    h = TempHistory(path=None, min_interval=60)
    h.record(19, 37, False, ts=1000)
    assert h.record(19, 37, False, ts=1030) is None  # same temp, <60s
    assert len(h.recent(now=1030)) == 1


def test_records_when_temp_changes_even_if_fast():
    h = TempHistory(path=None, min_interval=60)
    h.record(19, 37, False, ts=1000)
    assert h.record(20, 37, True, ts=1005) is not None  # changed temp
    assert len(h.recent(now=1005)) == 2


def test_records_after_interval_even_if_unchanged():
    h = TempHistory(path=None, min_interval=60)
    h.record(19, 37, False, ts=1000)
    assert h.record(19, 37, False, ts=1100) is not None  # >60s
    assert len(h.recent(now=1100)) == 2


def test_error_frame_not_recorded():
    h = TempHistory(path=None)
    assert h.record(None, 37, False, ts=1000) is None
    assert h.recent(now=1000) == []


def test_air_stored_when_provided():
    h = TempHistory(path=None, min_interval=0)
    p1 = h.record(19, 37, True, ts=1000, air=11.4)
    assert p1["air"] == 11.4
    # omitted air -> key absent (back-compatible with old rows)
    p2 = h.record(20, 37, True, ts=1001)
    assert "air" not in p2


def test_recent_filters_by_window():
    h = TempHistory(path=None, min_interval=0)
    h.record(18, 37, False, ts=1000)
    h.record(19, 37, False, ts=1000 + 3600)
    # only the last hour, relative to now=1000+7200
    pts = h.recent(hours=1.0, now=1000 + 7200)
    assert [p["cur"] for p in pts] == [19]


def test_retention_prunes_old(tmp_path):
    f = tmp_path / "h.jsonl"
    h = TempHistory(path=f, retention_hours=1, min_interval=0)
    h.record(18, 37, False, ts=1000)            # will age out
    h.record(19, 37, False, ts=1000 + 7200)     # now; prunes the old one
    assert [p["cur"] for p in h.recent(now=1000 + 7200)] == [19]


def test_append_recreates_missing_dir(tmp_path):
    import shutil
    import time

    d = tmp_path / "state"
    f = d / "history.jsonl"
    h = TempHistory(path=f, min_interval=0)
    base = time.time()
    h.record(19, 37, False, ts=base)
    shutil.rmtree(d)  # dir yanked out from under the running process
    assert not d.exists()
    h.record(20, 37, False, ts=base + 1)  # must self-heal, not raise
    assert f.exists()
    h2 = TempHistory(path=f, min_interval=0)
    assert h2.recent(hours=999, now=base + 1)[-1]["cur"] == 20


def test_persists_across_instances(tmp_path):
    import time

    f = tmp_path / "h.jsonl"
    base = time.time()  # _load() prunes vs wall-clock, so anchor near now
    h1 = TempHistory(path=f, min_interval=0)
    h1.record(19, 37, False, ts=base - 1)
    h1.record(20, 38, True, ts=base)
    h2 = TempHistory(path=f, min_interval=0)  # reload from disk
    pts = h2.recent(hours=999, now=base)
    assert [p["cur"] for p in pts] == [19, 20]
    assert pts[-1]["set"] == 38 and pts[-1]["heat"] is True


def test_startup_prune_rewrites_lazily(tmp_path):
    import json
    import time

    f = tmp_path / "h.jsonl"
    base = time.time()
    old = {"t": base - 7200, "cur": 18, "set": 37, "heat": False}
    keep = {"t": base, "cur": 19, "set": 37, "heat": False}
    f.write_text(json.dumps(old) + "\n" + json.dumps(keep) + "\n")

    h = TempHistory(path=f, retention_hours=1, min_interval=0)
    assert h.recent(hours=999, now=base) == [keep]
    assert f.read_text().count("\n") == 2

    h.record(20, 37, False, ts=base + 1)
    rows = [json.loads(line) for line in f.read_text().splitlines()]
    assert [row["cur"] for row in rows] == [19, 20]
