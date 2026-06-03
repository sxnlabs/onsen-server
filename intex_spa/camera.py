"""Camera bridge — snapshot loop + timelapse.

Mirrors `weather.py`: a cached, fail-soft subsystem wired into `create_app` via
lifespan tasks. The master switch is `load_config()` — when `state/camera.json`
is missing or `rtsps_url` is empty, every consumer in this module sees `None`
and degrades cleanly: no background tasks start, endpoints return
`{"enabled": false}`, the UI hides the camera card. The core path here is stdlib
+ a system `ffmpeg`; optional image-analysis deps live in companion modules with
guarded imports.

ffmpeg runs as a subprocess from a worker thread (`asyncio.to_thread`) so the
event loop is never blocked. Each grab writes `state/cam.jpg` atomically
(`tmp + replace`). On any failure the previous frame stays — same
stale-but-useful pattern as the spa supervisor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

_LOG = logging.getLogger("intex_spa.camera")


# -- config -------------------------------------------------------------------
DEFAULT_CONFIG = {
    "rtsps_url": "",
    "poll_seconds": 10.0,
    "roi": None,                            # cover-detection ROI: {x,y,w,h} or None
    "cover_baseline_on": None,              # {luma, std} captured by the calibrate endpoint
    "cover_baseline_off": None,             # idem; both present ⇒ nearest-baseline classifier
    "cover_forced_state": None,             # "on"|"off" overrides the classifier; null = auto
    "frame_path": "state/cam.jpg",
    "history_dir": "state/cam_history",
    "cover_state_path": "state/cover_state.json",
    "timelapse_every_seconds": 60.0,        # archive ≈ 1 frame/min
    "timelapse_retention_days": 7,
    "timelapse_fps": 24,
    "jpeg_quality": 7,                      # ffmpeg -q:v (2 best … 31 worst)
    "snapshot_max_width": 1280,             # downscale ≥4K source — fun, not surveillance
    "ffmpeg": "ffmpeg",
    "ffmpeg_extra_args": [],                # set by Step 0 if the default invocation fails
}


def load_config(path: str | Path | None) -> dict | None:
    """Read `state/camera.json` and return a merged config, or None if disabled.

    Disabled means: file missing, file malformed, or `rtsps_url` empty. The
    caller treats None as "camera subsystem off" — no tasks, no endpoints.
    """
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _LOG.warning("camera config %s unreadable: %s", p, e)
        return None
    if not isinstance(data, dict) or not (data.get("rtsps_url") or "").strip():
        return None
    data = {k: v for k, v in data.items() if k in DEFAULT_CONFIG}
    merged = {**DEFAULT_CONFIG, **data}
    merged["_path"] = str(p)
    return merged


def save_config(path: str | Path, data: dict) -> None:
    """Persist `state/camera.json` atomically. Strips internal keys (leading _)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in data.items() if k in DEFAULT_CONFIG and not k.startswith("_")}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(clean, indent=2))
    tmp.replace(p)


# -- snapshot loop ------------------------------------------------------------
class CameraSnapshot:
    """Periodic ffmpeg grab → `state/cam.jpg`. Also archives 1/min for timelapse.

    `extra_args` is a place for any RTSP knob Step 0 had to add (e.g. dropping
    `?enableSrtp` from the URL is handled by the caller; transport flags go here).
    """

    def __init__(
        self,
        rtsps_url: str,
        *,
        frame_path: str | Path = "state/cam.jpg",
        history_dir: str | Path = "state/cam_history",
        poll_seconds: float = 10.0,
        timelapse_every_seconds: float = 60.0,
        timelapse_retention_days: int = 7,
        timelapse_fps: int = 24,
        jpeg_quality: int = 7,
        snapshot_max_width: int | None = 1280,
        ffmpeg_bin: str = "ffmpeg",
        ffmpeg_extra_args: list[str] | None = None,
        grab_timeout: float = 30.0,
        post_grab: "callable | None" = None,
    ) -> None:
        self.url = rtsps_url
        self.frame_path = Path(frame_path)
        self.history_dir = Path(history_dir)
        self.poll = float(poll_seconds)
        self.timelapse_every = float(timelapse_every_seconds)
        self.retention_days = int(timelapse_retention_days)
        self.timelapse_fps = int(timelapse_fps)
        self.jpeg_quality = int(jpeg_quality)
        self.snapshot_max_width = int(snapshot_max_width) if snapshot_max_width else None
        self.ffmpeg_bin = ffmpeg_bin
        self.ffmpeg_extra_args = list(ffmpeg_extra_args or [])
        self.grab_timeout = grab_timeout
        # post_grab(frame_path) runs in a worker thread after each successful
        # grab — keeps cover-detection decoupled from this module.
        self.post_grab = post_grab
        self.last_frame_at: float | None = None
        self.last_error: str | None = None
        self._last_archive: float = 0.0
        self._last_prune: float = 0.0
        self._task: asyncio.Task | None = None

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # -- main loop --------------------------------------------------------
    async def _loop(self) -> None:
        # one immediate grab so the UI lights up quickly
        await self._tick()
        while True:
            await asyncio.sleep(self.poll)
            await self._tick()

    async def _tick(self) -> None:
        try:
            ok = await asyncio.to_thread(self._grab_once)
        except Exception:  # noqa: BLE001 — never let the loop die
            _LOG.exception("camera: grab raised")
            return
        if not ok:
            return
        now = time.time()
        self.last_frame_at = now
        self.last_error = None
        if now - self._last_archive >= self.timelapse_every:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._archive_frame, now)
            self._last_archive = now
        # opportunistic retention sweep (≈ hourly)
        if now - self._last_prune >= 3600:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._prune_history)
            self._last_prune = now
        if self.post_grab is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.post_grab, self.frame_path)

    # -- ffmpeg -----------------------------------------------------------
    def build_cmd(self, dest: Path) -> list[str]:
        """Default invocation: TCP transport, single frame, mid-quality JPEG.

        The default ffmpeg transport choice is unreliable over TLS; TCP is
        stable for RTSPS and regular RTSP cameras.
        """
        cmd = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            *self.ffmpeg_extra_args,
            "-y",
            "-i", self.url,
            "-frames:v", "1",
            # Force the output muxer — without this, ffmpeg picks format from
            # the extension, and our atomic-write tmp file is `cam.jpg.tmp`
            # → "Unable to choose an output format" failure.
            "-f", "image2",
        ]
        if self.snapshot_max_width:
            # `min(W, iw)` so we never upscale a smaller source; `-2` keeps the
            # aspect ratio and rounds to an even height (jpeg wants that).
            cmd += ["-vf", f"scale='min({self.snapshot_max_width},iw)':-2"]
        cmd += ["-q:v", str(self.jpeg_quality), str(dest)]
        return cmd

    def _grab_once(self) -> bool:
        """Blocking. Returns True iff a fresh frame is now at `self.frame_path`."""
        self.frame_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.frame_path.with_suffix(self.frame_path.suffix + ".tmp")
        try:
            proc = subprocess.run(
                self.build_cmd(tmp),
                capture_output=True,
                timeout=self.grab_timeout,
            )
        except FileNotFoundError:
            self.last_error = f"ffmpeg not found ({self.ffmpeg_bin})"
            return False
        except subprocess.TimeoutExpired:
            self.last_error = "ffmpeg timeout"
            with contextlib.suppress(OSError):
                tmp.unlink()
            return False
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            self.last_error = (err[:240] or f"ffmpeg exit {proc.returncode}")
            with contextlib.suppress(OSError):
                tmp.unlink()
            return False
        tmp.replace(self.frame_path)
        return True

    # -- timelapse archive + prune ---------------------------------------
    def _archive_frame(self, now: float) -> None:
        if not self.frame_path.exists():
            return
        dt = datetime.fromtimestamp(now)
        day_dir = self.history_dir / dt.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        dest = day_dir / (dt.strftime("%H-%M-%S") + ".jpg")
        try:
            os.link(self.frame_path, dest)  # APFS hard-link, ~free
        except OSError:
            shutil.copy2(self.frame_path, dest)
        # invalidate any cached mp4 for today — a frame was added
        with contextlib.suppress(OSError):
            (self.history_dir / (dt.strftime("%Y-%m-%d") + ".mp4")).unlink()

    def _prune_history(self) -> None:
        if not self.history_dir.exists():
            return
        cutoff = (datetime.now() - timedelta(days=self.retention_days)).date()
        for entry in self.history_dir.iterdir():
            try:
                d = datetime.strptime(entry.name.rstrip(".mp4"), "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < cutoff:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        entry.unlink()

    # -- timelapse generation --------------------------------------------
    def generate_timelapse(self, date: str) -> Path | None:
        """Build (and cache) `state/cam_history/<date>.mp4`. Returns its path or None.

        Re-builds if the cached mp4 is older than the newest frame for that day —
        `_archive_frame` deletes the stale mp4 on each new frame, so a fresh
        request will always pick up new material.
        """
        day_dir = self.history_dir / date
        if not day_dir.is_dir():
            return None
        frames = sorted(p for p in day_dir.iterdir() if p.suffix == ".jpg")
        if not frames:
            return None
        out = self.history_dir / (date + ".mp4")
        if out.exists():
            return out
        # ffmpeg concat via glob pattern requires sorted lexicographic names — we
        # write them HH-MM-SS so that's already the case.
        listfile = day_dir / ".concat.txt"
        listfile.write_text("".join(f"file '{p.name}'\nduration {1 / self.timelapse_fps}\n"
                                    for p in frames))
        cmd = [
            self.ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(listfile),
            "-vsync", "vfr",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264",
            "-crf", "23",
            str(out),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            _LOG.warning("camera: timelapse ffmpeg failed (%s)", e)
            with contextlib.suppress(OSError):
                listfile.unlink()
            return None
        with contextlib.suppress(OSError):
            listfile.unlink()
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            _LOG.warning("camera: timelapse ffmpeg exit %d: %s",
                         proc.returncode, proc.stderr.decode("utf-8", "replace")[:240])
            with contextlib.suppress(OSError):
                out.unlink()
            return None
        return out

    # -- read helpers -----------------------------------------------------
    def snapshot(self) -> dict:
        now = time.time()
        return {
            "frame_at": self.last_frame_at,
            "age_s": round(now - self.last_frame_at, 1) if self.last_frame_at else None,
            "error": self.last_error,
            "poll_seconds": self.poll,
            "history_days": self.retention_days,
        }
