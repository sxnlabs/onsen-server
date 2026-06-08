"""Supervisor: owns the single spa client, polls it, and fans out state via SSE.

Why a supervisor at all: the firmware allows only one TCP client, so the whole app
must funnel through exactly one IntexSpaClient. The supervisor holds it, runs a
periodic poll (which doubles as the keepalive the firmware needs), keeps the last
known status on error, and pushes every state change to SSE subscribers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

from .client import IntexSpaClient, SpaUnreachable
from .command_log import CommandLog
from .history import TempHistory
from .protocol import PORT

_LOG = logging.getLogger("intex_spa.supervisor")


class Supervisor:
    def __init__(
        self,
        host: str,
        port: int = PORT,
        poll_interval: float = 10.0,
        history: TempHistory | None = None,
        air_provider: Callable[[], float | None] | None = None,
        command_log: CommandLog | None = None,
        pause_path: str | Path | None = None,
    ) -> None:
        self.poll_interval = poll_interval
        self.history = history if history is not None else TempHistory(path=None)
        self.command_log = command_log
        self.pause_path = Path(pause_path) if pause_path else None
        self.client = IntexSpaClient(host, port=port, command_recorder=self._log_wire_command)
        # returns the current outside air temp (cached, non-blocking) or None
        self.air_provider = air_provider
        # `paused` halts comfort automation writes, but polling continues: status
        # reads are the safety feed and keepalive. User-initiated set_field /
        # set_preset still go through (explicit intent).
        self.paused: bool = self._load_paused()
        self.state: dict = {
            "status": None,      # last decoded status dict, or None until first read
            "online": False,
            "error": None,
            "updated_at": None,  # epoch seconds
        }
        self._subs: set[asyncio.Queue] = set()
        self._poll_task: asyncio.Task | None = None

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self.client.close()

    # -- SSE subscriptions ----------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subs.add(q)
        q.put_nowait(self.state)  # push current snapshot immediately
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _publish(self) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(self.state)
            except asyncio.QueueFull:  # slow consumer: drop oldest, keep newest
                try:
                    q.get_nowait()
                    q.put_nowait(self.state)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _set_state(self, *, online: bool, error: str | None, status: dict | None = None) -> None:
        # keep the last known status on error so the UI can show a stale-but-useful view
        kept = status if status is not None else self.state.get("status")
        self.state = {
            "status": kept,
            "online": online,
            "error": error,
            "updated_at": time.time(),
        }
        if online and kept and kept.get("current_temp") is not None:
            try:
                air = None
                if self.air_provider is not None:
                    try:
                        air = self.air_provider()
                    except Exception:  # noqa: BLE001 — weather is best-effort
                        air = None
                self.history.record(
                    kept["current_temp"],
                    kept.get("preset_temp"),
                    kept.get("heater"),
                    air=air,
                    filter_on=kept.get("filter"),
                )
            except Exception:  # noqa: BLE001 — history is best-effort, never break a refresh
                _LOG.exception("history record failed (non-fatal)")
        self._publish()

    # -- pause control ------------------------------------------------------
    def set_paused(self, on: bool) -> None:
        """Pause/resume background polling. User actions (set_field / set_preset)
        keep working — pause only halts automated traffic to the controller."""
        was = self.paused
        self.paused = bool(on)
        self._save_paused()
        if was and not self.paused:
            # On resume, push a fresh state event so SSE subscribers refresh.
            self._publish()

    def _load_paused(self) -> bool:
        if self.pause_path is None or not self.pause_path.exists():
            return False
        try:
            raw = json.loads(self.pause_path.read_text())
        except (OSError, json.JSONDecodeError):
            _LOG.warning("failed to read pause state", exc_info=True)
            return False
        return bool(raw.get("paused")) if isinstance(raw, dict) else False

    def _save_paused(self) -> None:
        if self.pause_path is None:
            return
        try:
            self.pause_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.pause_path.with_suffix(self.pause_path.suffix + ".tmp")
            tmp.write_text(json.dumps({"paused": self.paused}))
            tmp.replace(self.pause_path)
        except OSError:
            _LOG.warning("failed to persist pause state", exc_info=True)

    # -- operations -----------------------------------------------------------
    async def refresh(self) -> dict:
        try:
            st = await self.client.status()
            self._set_state(status=st, online=True, error=None)
        except SpaUnreachable as e:
            self._set_state(online=False, error=str(e))
        except Exception as e:  # noqa: BLE001 — never let the poll loop die
            _LOG.exception("unexpected error refreshing spa status")
            self._set_state(online=False, error=str(e))
        return self.state

    async def set_field(self, field: str, desired: bool) -> dict:
        before = self.state.get("status")
        try:
            st = await self.client.set(field, desired)
        except (SpaUnreachable, ValueError) as e:
            self._log_command("toggle", field=field, desired=desired, before=before, error=str(e))
            if isinstance(e, SpaUnreachable):
                self._set_state(online=False, error=str(e))
            raise
        except Exception as e:
            self._log_command("toggle", field=field, desired=desired, before=before, error=str(e))
            raise
        self._log_command("toggle", field=field, desired=desired, before=before, after=st)
        self._set_state(status=st, online=True, error=None)
        return self.state

    async def set_preset(self, temp: int) -> dict:
        before = self.state.get("status")
        try:
            st = await self.client.set_preset(temp)
        except (SpaUnreachable, ValueError) as e:
            self._log_command("preset", temp=temp, before=before, error=str(e))
            if isinstance(e, SpaUnreachable):
                self._set_state(online=False, error=str(e))
            raise
        except Exception as e:
            self._log_command("preset", temp=temp, before=before, error=str(e))
            raise
        self._log_command("preset", temp=temp, before=before, after=st)
        self._set_state(status=st, online=True, error=None)
        return self.state

    def _log_command(self, kind: str, **entry) -> None:
        if self.command_log is None:
            return
        try:
            self.command_log.record(kind=kind, **entry)
        except Exception:  # noqa: BLE001 — logging is audit-only, never block spa control
            _LOG.exception("command audit log failed (non-fatal)")

    def _log_wire_command(self, **entry) -> None:
        self._log_command("wire", **entry)

    async def _poll_loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.poll_interval)
