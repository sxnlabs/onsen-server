"""Async TCP client for a single Intex PureSpa wifi module.

The firmware tolerates only ONE TCP client on :8990, so this class is meant to be
a singleton (the Supervisor owns exactly one). An internal lock serializes every
round-trip; the connection is persistent and lazily (re)established.

Commands are toggles, so `set()` reads status first and only sends when the current
state differs from the desired one (idempotent). `set_preset()` is absolute.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from . import protocol

_LOG = logging.getLogger("intex_spa.client")


class SpaUnreachable(Exception):
    """Raised when the spa can't be reached after retries."""


class IntexSpaClient:
    def __init__(
        self,
        host: str,
        port: int = protocol.PORT,
        timeout: float = 8.0,
        retries: int = 2,
        command_recorder: Callable[..., None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.command_recorder = command_recorder
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    # -- connection management ------------------------------------------------
    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout
        )
        _LOG.info("connected to spa %s:%s", self.host, self.port)

    async def _disconnect(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass

    def _is_broken(self) -> bool:
        return (
            self._writer is None
            or self._reader is None
            or self._writer.is_closing()
            or self._reader.at_eof()
        )

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect()

    # -- single round-trip (assumes lock held) --------------------------------
    async def _roundtrip(
        self, intent: str, preset_temp: int | None = None, *, idempotent: bool = True
    ) -> dict:
        """One request/response. Retries only when re-sending is safe.

        `status` and `preset_temp` are idempotent on the wire, so a lost reply can
        be retried by re-sending the same frame. Toggle commands are NOT — pass
        `idempotent=False` to send exactly once and let the caller (`_toggle_to`)
        decide what to do, by re-reading state, after a failure.
        """
        req, sid = protocol.build_request(intent, preset_temp)
        attempts = self.retries + 1 if idempotent else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                if self._is_broken():
                    await self._disconnect()
                    await self._connect()
                self._writer.write(req)
                await asyncio.wait_for(self._writer.drain(), timeout=self.timeout)
                if intent != "status" and self.command_recorder is not None:
                    try:
                        self.command_recorder(
                            intent=intent,
                            preset_temp=preset_temp,
                            idempotent=idempotent,
                            attempt=attempt + 1,
                        )
                    except Exception:  # noqa: BLE001 — audit only, never block control
                        _LOG.exception("command recorder failed (non-fatal)")
                line = await asyncio.wait_for(self._reader.readline(), timeout=self.timeout)
                if not line:
                    raise ConnectionError("empty response (peer closed)")
                return protocol.parse_response(line, expected_sid=sid)
            except protocol.SpaProtocolError as e:
                # malformed reply: retry without tearing the socket down
                last_exc = e
                _LOG.warning("protocol error (attempt %d): %s", attempt + 1, e)
                if idempotent:
                    await asyncio.sleep(0.5)
            except (OSError, asyncio.TimeoutError) as e:
                last_exc = e
                _LOG.warning("network error (attempt %d): %s", attempt + 1, e)
                await self._disconnect()
                if idempotent:
                    await asyncio.sleep(0.5)
        raise SpaUnreachable(f"{intent} failed after {attempts} attempts: {last_exc}")

    async def _toggle_to(self, field: str, desired: bool, st: dict) -> dict:
        """Drive a toggle field to `desired`, safely over a flaky link.

        A toggle inverts state on the wire, so re-sending one after a lost/failed
        ack is dangerous: the command may have landed, and the resend flips it
        straight back — that's how a marginal connection cycles the relay. So we
        only ever fire a toggle when a *fresh* status read says it's still wrong,
        send a single shot, then re-read to learn the real outcome.
        """
        for _ in range(self.retries + 1):
            if bool(st.get(field)) == bool(desired):
                return st  # already there — a toggle here would flip it the wrong way
            try:
                st = await self._roundtrip(field, idempotent=False)
            except SpaUnreachable:
                st = await self._roundtrip("status")  # ack lost? observe ground truth
        if bool(st.get(field)) != bool(desired):
            raise SpaUnreachable(f"could not drive {field} to {desired!r}")
        return st

    # -- public API (each takes the lock) -------------------------------------
    async def status(self) -> dict:
        async with self._lock:
            return await self._roundtrip("status")

    async def set(self, field: str, desired: bool) -> dict:
        if field not in protocol.TOGGLE_FIELDS:
            raise ValueError(f"not a toggle field: {field!r}")
        async with self._lock:
            st = await self._roundtrip("status")
            if field == "heater" and desired and st.get("current_temp") is None:
                raise ValueError("refusing to enable heater without a valid water temperature")
            # Safety interlock: the heater must never run without circulation.
            # Enforce "heater on => filter on" on EVERY write path (manual UI
            # included), not just the scheduler's decision engine.
            if field == "heater" and desired and not st.get("filter"):
                st = await self._toggle_to("filter", True, st)  # circulation first
            elif field == "filter" and not desired and st.get("heater"):
                st = await self._toggle_to("heater", False, st)  # cut heat first
            return await self._toggle_to(field, desired, st)

    async def set_preset(self, temp: int) -> dict:
        if not (protocol.TEMP_MIN_C <= temp <= protocol.TEMP_MAX_C):
            raise ValueError(
                f"temp {temp} out of range [{protocol.TEMP_MIN_C}, {protocol.TEMP_MAX_C}]"
            )
        async with self._lock:
            st = await self._roundtrip("status")
            if st.get("preset_temp") == temp:
                return st
            return await self._roundtrip("preset_temp", temp)
