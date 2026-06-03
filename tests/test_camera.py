"""Offline tests for the camera subsystem.

No camera, no ffmpeg on the wire — every external call is mocked. These tests pin:
  - the master-switch contract (no config ⇒ off, no surprises)
  - load_config / save_config round-trip and validation
  - CameraSnapshot._grab_once subprocess handling (success / non-zero / missing)
  - atomic frame write (the tmp + replace pattern)
  - HTTP endpoints in both modes (off vs on-but-no-frame)
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from fake_spa import FakeSpa
from intex_spa import camera as cam_mod
from intex_spa.camera import CameraSnapshot
from web.main import create_app


# -- helpers -----------------------------------------------------------------
@asynccontextmanager
async def app_for(spa: FakeSpa, **kw):
    host, port = await spa.start()
    kw.setdefault("weather_enabled", False)
    kw.setdefault("camera_config_path", None)  # off by default
    app = create_app(host, port=port, poll_interval=9999,
                     history_path=None, schedule_path=None, command_log_path=None, **kw)
    await app.state.supervisor.refresh()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            c.app = app
            yield c
    finally:
        await app.state.supervisor.client.close()
        await spa.stop()


def _write_config(path: Path, **overrides):
    base = {
        "rtsps_url": "rtsps://camera.local/test",
        "poll_seconds": 10,
        "roi": None,
    }
    base.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(base))


# -- load_config / save_config -----------------------------------------------
def test_load_config_missing_returns_none(tmp_path):
    assert cam_mod.load_config(None) is None
    assert cam_mod.load_config(tmp_path / "absent.json") is None


def test_load_config_empty_url_returns_none(tmp_path):
    p = tmp_path / "camera.json"
    p.write_text(json.dumps({"rtsps_url": ""}))
    assert cam_mod.load_config(p) is None


def test_load_config_malformed_returns_none(tmp_path):
    p = tmp_path / "camera.json"
    p.write_text("{not json")
    assert cam_mod.load_config(p) is None


def test_load_config_fills_defaults(tmp_path):
    p = tmp_path / "camera.json"
    _write_config(p, rtsps_url="rtsps://x/y", poll_seconds=5)
    cfg = cam_mod.load_config(p)
    assert cfg is not None
    assert cfg["rtsps_url"] == "rtsps://x/y"
    assert cfg["poll_seconds"] == 5
    # defaults from DEFAULT_CONFIG
    assert cfg["jpeg_quality"] == 7
    assert cfg["timelapse_retention_days"] == 7
    assert cfg["frame_path"].endswith("cam.jpg")


def test_load_config_drops_legacy_protect_keys(tmp_path):
    p = tmp_path / "camera.json"
    _write_config(
        p,
        protect={"host": "1.2.3.4", "user": "u", "pass": "p"},
        usage_path="state/usage.jsonl",
    )
    cfg = cam_mod.load_config(p)
    assert "protect" not in cfg
    assert "usage_path" not in cfg


def test_save_config_strips_internal_keys(tmp_path):
    p = tmp_path / "camera.json"
    cam_mod.save_config(
        p,
        {
            "rtsps_url": "rtsps://x/y",
            "_path": "leak",
            "protect": {"host": "legacy"},
            "roi": {"x": 1, "y": 2, "w": 3, "h": 4},
        },
    )
    on_disk = json.loads(p.read_text())
    assert "_path" not in on_disk
    assert "protect" not in on_disk
    assert on_disk["roi"] == {"x": 1, "y": 2, "w": 3, "h": 4}


# -- CameraSnapshot._grab_once ----------------------------------------------
class _CompletedProcess:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


def test_grab_once_writes_atomically(tmp_path):
    frame = tmp_path / "cam.jpg"
    snap = CameraSnapshot("rtsps://x", frame_path=frame, history_dir=tmp_path / "hist")

    def fake_run(cmd, **kw):
        # subprocess "wrote" the temp file — find the last arg (output path)
        out = Path(cmd[-1])
        out.write_bytes(b"\xff\xd8\xff" + b"fake-jpeg-bytes")
        # the live frame doesn't exist yet at this point — it's renamed below
        assert not frame.exists() or frame.read_bytes() == b""
        return _CompletedProcess(returncode=0)

    with patch("intex_spa.camera.subprocess.run", side_effect=fake_run):
        assert snap._grab_once() is True
    assert frame.exists() and frame.read_bytes().startswith(b"\xff\xd8\xff")
    # tmp shouldn't remain after rename
    assert not (frame.with_suffix(frame.suffix + ".tmp")).exists()


def test_grab_once_keeps_old_frame_on_failure(tmp_path):
    frame = tmp_path / "cam.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"OLD")
    snap = CameraSnapshot("rtsps://x", frame_path=frame, history_dir=tmp_path / "hist")
    with patch("intex_spa.camera.subprocess.run",
               return_value=_CompletedProcess(returncode=1, stderr=b"refused")):
        assert snap._grab_once() is False
    assert snap.last_error and "refused" in snap.last_error
    # untouched
    assert frame.read_bytes() == b"OLD"


def test_grab_once_handles_timeout(tmp_path):
    snap = CameraSnapshot("rtsps://x", frame_path=tmp_path / "cam.jpg",
                          history_dir=tmp_path / "hist", grab_timeout=0.01)
    with patch("intex_spa.camera.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=0.01)):
        assert snap._grab_once() is False
    assert snap.last_error == "ffmpeg timeout"


def test_grab_once_handles_missing_ffmpeg(tmp_path):
    snap = CameraSnapshot("rtsps://x", frame_path=tmp_path / "cam.jpg",
                          history_dir=tmp_path / "hist", ffmpeg_bin="absent-bin")
    with patch("intex_spa.camera.subprocess.run", side_effect=FileNotFoundError):
        assert snap._grab_once() is False
    assert snap.last_error and "not found" in snap.last_error


def test_build_cmd_uses_tcp_transport_and_quality(tmp_path):
    snap = CameraSnapshot("rtsps://x?enableSrtp", frame_path=tmp_path / "cam.jpg",
                          history_dir=tmp_path / "hist", jpeg_quality=9)
    cmd = snap.build_cmd(tmp_path / "out.jpg")
    assert "-rtsp_transport" in cmd and "tcp" in cmd
    assert cmd[cmd.index("-q:v") + 1] == "9"
    assert "rtsps://x?enableSrtp" in cmd
    # force output muxer so cam.jpg.tmp doesn't break ffmpeg's format guess
    assert cmd[cmd.index("-f") + 1] == "image2"
    # default downscale: 1280 max width, preserve aspect, never upscale
    assert "-vf" in cmd and "scale='min(1280,iw)':-2" in cmd[cmd.index("-vf") + 1]


def test_build_cmd_no_scale_when_max_width_none(tmp_path):
    snap = CameraSnapshot("rtsps://x", frame_path=tmp_path / "cam.jpg",
                          history_dir=tmp_path / "hist", snapshot_max_width=None)
    cmd = snap.build_cmd(tmp_path / "out.jpg")
    assert "-vf" not in cmd


# -- timelapse ----------------------------------------------------------------
def test_generate_timelapse_no_frames_returns_none(tmp_path):
    snap = CameraSnapshot("rtsps://x", frame_path=tmp_path / "cam.jpg",
                          history_dir=tmp_path / "hist")
    assert snap.generate_timelapse("2026-05-22") is None


def test_generate_timelapse_invokes_ffmpeg(tmp_path):
    hist = tmp_path / "hist"
    day = hist / "2026-05-22"
    day.mkdir(parents=True)
    (day / "10-00-00.jpg").write_bytes(b"jpg")
    (day / "10-01-00.jpg").write_bytes(b"jpg")
    snap = CameraSnapshot("rtsps://x", frame_path=tmp_path / "cam.jpg",
                          history_dir=hist)

    def fake_run(cmd, **kw):
        # third-to-last positional is the output mp4 path
        out = Path(cmd[-1])
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return _CompletedProcess(returncode=0)

    with patch("intex_spa.camera.subprocess.run", side_effect=fake_run):
        mp4 = snap.generate_timelapse("2026-05-22")
    assert mp4 is not None and mp4.exists()
    # second call returns cached mp4 without re-invoking ffmpeg
    with patch("intex_spa.camera.subprocess.run",
               side_effect=AssertionError("should not be called")):
        again = snap.generate_timelapse("2026-05-22")
    assert again == mp4


# -- cover_detect ------------------------------------------------------------
def test_cover_detect_no_roi_returns_unknown(tmp_path):
    from intex_spa import cover_detect
    out = cover_detect.classify(tmp_path / "missing.jpg", None)
    assert out["state"] == "unknown"
    assert out["luma"] is None


def test_cover_detect_no_frame_returns_unknown(tmp_path):
    from intex_spa import cover_detect
    out = cover_detect.classify(tmp_path / "missing.jpg", {"x": 0, "y": 0, "w": 10, "h": 10})
    assert out["state"] == "unknown"


def test_cover_detect_without_pillow_returns_unknown(tmp_path):
    from intex_spa import cover_detect
    with patch.object(cover_detect, "HAVE_DEPS", False):
        out = cover_detect.classify(tmp_path / "any.jpg", {"x": 0, "y": 0, "w": 10, "h": 10})
    assert out["state"] == "unknown"
    assert "pillow" in out["reason"].lower()


def test_cover_state_persists_round_trip(tmp_path):
    from intex_spa import cover_detect
    p = tmp_path / "cover.json"
    cover_detect.save_state(p, {"state": "on", "confidence": 0.8, "at": 12345})
    loaded = cover_detect.load_state(p)
    assert loaded == {"state": "on", "confidence": 0.8, "at": 12345}
    assert cover_detect.load_state(tmp_path / "absent.json") is None


# -- HTTP endpoints: OFF mode (no config) ------------------------------------
async def test_camera_status_disabled():
    spa = FakeSpa()
    async with app_for(spa) as c:
        r = await c.get("/api/camera/status")
        assert r.status_code == 200 and r.json() == {"enabled": False}


async def test_camera_jpg_404_when_disabled():
    spa = FakeSpa()
    async with app_for(spa) as c:
        assert (await c.get("/camera.jpg")).status_code == 404


async def test_timelapse_503_when_disabled():
    spa = FakeSpa()
    async with app_for(spa) as c:
        r = await c.get("/timelapse?date=2026-05-22")
        assert r.status_code == 503


async def test_camera_set_roi_503_when_disabled():
    spa = FakeSpa()
    async with app_for(spa) as c:
        r = await c.post("/api/camera/roi", json={"x": 0, "y": 0, "w": 10, "h": 10})
        assert r.status_code == 503


async def test_index_omits_camera_card_when_disabled():
    spa = FakeSpa()
    async with app_for(spa) as c:
        r = await c.get("/")
        assert "cam-card" not in r.text
        assert "/static/camera.js" not in r.text


# -- HTTP endpoints: ON mode (config exists, no real frame) ------------------
async def test_camera_status_enabled_no_frame(tmp_path):
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    _write_config(cfg_path, rtsps_url="rtsps://stub/x")
    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        r = await c.get("/api/camera/status")
        body = r.json()
        assert body["enabled"] is True
        assert body["frame_at"] is None
        assert body["roi"] is None


async def test_camera_status_does_not_import_cover_detector(tmp_path):
    sys.modules.pop("intex_spa.cover_detect", None)
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    cover_state = tmp_path / "cover_state.json"
    _write_config(
        cfg_path,
        rtsps_url="rtsps://stub/x",
        roi={"x": 1, "y": 2, "w": 3, "h": 4},
        cover_state_path=str(cover_state),
    )
    cover_state.write_text(json.dumps({"state": "on", "confidence": 0.9}))

    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        r = await c.get("/api/camera/status")
        assert r.json()["cover"]["state"] == "on"

    assert "intex_spa.cover_detect" not in sys.modules


async def test_camera_jpg_404_with_config_no_frame_yet(tmp_path):
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    _write_config(cfg_path, rtsps_url="rtsps://stub/x")
    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        assert (await c.get("/camera.jpg")).status_code == 404


async def test_camera_roi_save_and_clear(tmp_path):
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    _write_config(cfg_path, rtsps_url="rtsps://stub/x")
    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        r = await c.post("/api/camera/roi", json={"x": 10, "y": 20, "w": 100, "h": 80})
        assert r.status_code == 200 and r.json()["roi"]["x"] == 10
        # round-trips to disk
        assert json.loads(cfg_path.read_text())["roi"] == {"x": 10, "y": 20, "w": 100, "h": 80}
        # clear with explicit null body (json=None on httpx sends no body)
        r2 = await c.post("/api/camera/roi", content="null",
                          headers={"Content-Type": "application/json"})
        assert r2.status_code == 200 and r2.json()["roi"] is None
        assert json.loads(cfg_path.read_text())["roi"] is None


async def test_camera_roi_validation(tmp_path):
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    _write_config(cfg_path, rtsps_url="rtsps://stub/x")
    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        # missing keys
        assert (await c.post("/api/camera/roi", json={"x": 0})).status_code == 400
        # zero-size box
        assert (await c.post("/api/camera/roi", json={"x": 0, "y": 0, "w": 0, "h": 10})).status_code == 400


async def test_timelapse_date_validated(tmp_path):
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    _write_config(cfg_path, rtsps_url="rtsps://stub/x")
    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        r = await c.get("/timelapse?date=not-a-date")
        assert r.status_code == 400


async def test_index_includes_camera_card_when_enabled(tmp_path):
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    _write_config(cfg_path, rtsps_url="rtsps://stub/x")
    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        r = await c.get("/")
        assert "cam-card" in r.text
        assert "/static/camera.js" in r.text
        # default lang = EN ⇒ "Cover" label
        assert "Cover" in r.text
        # FR via Accept-Language ⇒ "Housse"
        r_fr = await c.get("/", headers={"Accept-Language": "fr-FR,fr;q=0.9"})
        assert "Housse" in r_fr.text


# -- cover state: forced override -------------------------------------------
async def test_cover_force_on_off_auto_round_trip(tmp_path):
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    _write_config(cfg_path, rtsps_url="rtsps://stub/x")
    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        # default is auto (None)
        assert (await c.get("/api/camera/status")).json().get("forced_state") in (None, "")

        # force ON
        r = await c.post("/api/camera/cover/state?state=on")
        assert r.status_code == 200 and r.json()["forced_state"] == "on"
        assert json.loads(cfg_path.read_text())["cover_forced_state"] == "on"

        # force OFF
        r2 = await c.post("/api/camera/cover/state?state=off")
        assert r2.json()["forced_state"] == "off"
        assert json.loads(cfg_path.read_text())["cover_forced_state"] == "off"

        # back to auto
        r3 = await c.post("/api/camera/cover/state?state=auto")
        assert r3.json()["forced_state"] is None
        assert json.loads(cfg_path.read_text())["cover_forced_state"] is None


async def test_cover_force_invalid_state_400(tmp_path):
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    _write_config(cfg_path, rtsps_url="rtsps://stub/x")
    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        assert (await c.post("/api/camera/cover/state?state=maybe")).status_code == 400


async def test_cover_force_503_when_disabled():
    spa = FakeSpa()
    async with app_for(spa) as c:  # no camera config
        assert (await c.post("/api/camera/cover/state?state=on")).status_code == 503


# -- cover_detect: forced_state short-circuits classification ---------------
def test_cover_detect_forced_state_returns_immediately(tmp_path):
    from intex_spa import cover_detect
    # No deps required for the forced path itself — but we want luma/std too.
    # Without pillow, luma/std stay None; state still locks to the forced value.
    out_on = cover_detect.classify(
        tmp_path / "absent.jpg", roi=None, forced_state="on",
    )
    assert out_on["state"] == "on"
    assert out_on["confidence"] == 1.0
    assert out_on["forced"] is True

    out_off = cover_detect.classify(
        tmp_path / "absent.jpg", roi=None, forced_state="off",
    )
    assert out_off["state"] == "off"
    assert out_off["forced"] is True

    # Invalid forced_state falls through to the normal path → unknown (no ROI)
    out_none = cover_detect.classify(
        tmp_path / "absent.jpg", roi=None, forced_state="maybe",
    )
    assert out_none["state"] == "unknown"
    assert out_none["forced"] is False


# -- cover_detect: baseline classifiers --------------------------------------
def test_cover_detect_nearest_baseline_picks_closer(tmp_path):
    """Both baselines set: classifier returns the closer one in (luma, std) space."""
    from unittest.mock import patch
    from intex_spa import cover_detect

    with patch.object(cover_detect, "HAVE_DEPS", True), \
         patch.object(cover_detect, "sample", return_value=(60.0, 15.0)):
        # baseline_on=(60,15) → distance 0, baseline_off=(200, 50) → ~145
        out = cover_detect.classify(
            tmp_path / "any.jpg",
            roi={"x": 0, "y": 0, "w": 10, "h": 10},
            baseline_on={"luma": 60, "std": 15},
            baseline_off={"luma": 200, "std": 50},
        )
    assert out["state"] == "on"
    assert "nearest baseline ON" in out["reason"]


def test_cover_detect_single_baseline_radius(tmp_path):
    """With only one baseline, classify trusts it when close, else stays unknown."""
    from unittest.mock import patch
    from intex_spa import cover_detect

    # Close to the ON baseline → state=on
    with patch.object(cover_detect, "HAVE_DEPS", True), \
         patch.object(cover_detect, "sample", return_value=(60.0, 15.0)):
        out = cover_detect.classify(
            tmp_path / "any.jpg",
            roi={"x": 0, "y": 0, "w": 10, "h": 10},
            baseline_on={"luma": 60, "std": 15},
        )
    assert out["state"] == "on"

    # Far from the ON baseline → unknown (need OFF baseline)
    with patch.object(cover_detect, "HAVE_DEPS", True), \
         patch.object(cover_detect, "sample", return_value=(180.0, 50.0)):
        out = cover_detect.classify(
            tmp_path / "any.jpg",
            roi={"x": 0, "y": 0, "w": 10, "h": 10},
            baseline_on={"luma": 60, "std": 15},
        )
    assert out["state"] == "unknown"
    assert "far from ON baseline" in out["reason"]


# -- cover calibrate / reset endpoints --------------------------------------
async def test_cover_calibrate_needs_roi(tmp_path):
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    _write_config(cfg_path, rtsps_url="rtsps://stub/x")  # roi=None
    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        r = await c.post("/api/camera/cover/calibrate?state=on")
        assert r.status_code == 400


async def test_cover_calibrate_invalid_state(tmp_path):
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    _write_config(cfg_path, rtsps_url="rtsps://stub/x",
                  roi={"x": 0, "y": 0, "w": 10, "h": 10})
    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        r = await c.post("/api/camera/cover/calibrate?state=neither")
        assert r.status_code == 400


async def test_cover_reset_clears_baselines(tmp_path):
    spa = FakeSpa()
    cfg_path = tmp_path / "camera.json"
    _write_config(cfg_path, rtsps_url="rtsps://stub/x",
                  cover_baseline_on={"luma": 60, "std": 15, "at": 1},
                  cover_baseline_off={"luma": 200, "std": 50, "at": 2})
    async with app_for(spa, camera_config_path=str(cfg_path)) as c:
        r = await c.post("/api/camera/cover/reset")
        assert r.status_code == 200
        on_disk = json.loads(cfg_path.read_text())
        assert on_disk["cover_baseline_on"] is None
        assert on_disk["cover_baseline_off"] is None
