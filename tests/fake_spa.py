"""A fake Intex spa TCP server for offline e2e tests.

Speaks the real wire protocol: accepts line-delimited JSON requests, applies
toggle/preset semantics to an in-memory state, and replies with a checksum-valid
status frame. Used to drive IntexSpaClient without real hardware.
"""

from __future__ import annotations

import asyncio
import json

from intex_spa import protocol

# reverse map: hex request prefix -> intent
_PREFIX_TO_INTENT = {v: k for k, v in protocol.COMMANDS.items()}


def encode_frame(state: dict) -> str:
    """Build a 38-char status data string (with trailing checksum) from a state dict."""
    raw = 0
    for name, off in protocol._BIT_OFFSET.items():
        if state.get(name):
            raw |= 1 << off
    raw |= (state["current_temp"] & 0xFF) << 88
    raw |= (state["preset_temp"] & 0xFF) << 24
    payload36 = format(raw, "038X")[:36]  # 18 payload bytes; low (checksum) byte dropped
    return payload36 + format(protocol.checksum_int(payload36), "02X")


class FakeSpa:
    """In-memory spa. Start it, point IntexSpaClient at (host, port), drive it."""

    DEFAULT_STATE = {
        "power": True,
        "filter": False,
        "heater": False,
        "jets": False,
        "bubbles": False,
        "sanitizer": False,
        "current_temp": 19,
        "preset_temp": 37,
    }

    def __init__(self, state: dict | None = None) -> None:
        self.state = dict(state or self.DEFAULT_STATE)
        self.requests: list[dict] = []           # raw request dicts received
        self.intents: list[str | None] = []      # decoded intent per request
        # fault injection: intent -> count of replies to return as result:'timeout'
        # AFTER applying the state change -- models the real module's behaviour on
        # a flaky link (the relay actuates but the ack is lost).
        self.timeout_on: dict[str, int] = {}
        self.host = "127.0.0.1"
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._conns: set[asyncio.StreamWriter] = set()

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.host, self.port

    async def stop(self) -> None:
        # close listener AND any live connections (server.close() alone leaves
        # established sockets open, which would keep a persistent client online)
        for w in list(self._conns):
            w.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Requests arrive as bare JSON objects with NO trailing newline (matching the
        # real firmware), so accumulate bytes until one parses rather than readline().
        buf = b""
        self._conns.add(writer)
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buf += chunk
                try:
                    req = json.loads(buf.decode())
                except json.JSONDecodeError:
                    continue  # partial frame, read more
                buf = b""
                self.requests.append(req)
                intent = self._match_intent(req["data"])
                self.intents.append(intent)
                self._apply(intent, req["data"])
                result = "ok"
                if self.timeout_on.get(intent, 0) > 0:
                    self.timeout_on[intent] -= 1
                    result = "timeout"  # state already applied; ack is "lost"
                resp = {
                    "sid": req["sid"],
                    "data": encode_frame(self.state),
                    "result": result,
                    "type": protocol.TYPE_STATUS,
                }
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            self._conns.discard(writer)
            writer.close()

    def _match_intent(self, data_hex: str) -> str | None:
        # data = prefix (+ optional preset byte) + checksum; match longest prefix
        for prefix, intent in sorted(_PREFIX_TO_INTENT.items(), key=lambda kv: -len(kv[0])):
            if data_hex.startswith(prefix):
                return intent
        return None

    def _apply(self, intent: str | None, data_hex: str) -> None:
        if intent in protocol.TOGGLE_FIELDS:
            self.state[intent] = not self.state[intent]
        elif intent == "preset_temp":
            prefix = protocol.COMMANDS["preset_temp"]
            # temp is always the 2 hex chars right after the prefix (range 0x14..0x28)
            temp_hex = data_hex[len(prefix) : len(prefix) + 2]
            self.state["preset_temp"] = int(temp_hex, 16)
        # "status" / None -> no mutation
