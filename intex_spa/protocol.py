"""Intex PureSpa wire protocol — pure, dependency-free encode/decode.

Verified byte-exact against mathieu-mp/aio-intex-spa's published test vectors
(see tests/test_protocol.py) and against the real device on 2026-05-22.

The wifi module speaks line-delimited JSON over TCP/8990, no auth, no crypto:

    request : {"data": "<hex><checksum>", "sid": "<ts>", "type": 1}
    response: {"sid": "<ts>", "data": "<hex>", "result": "ok", "type": 2}

`sid` is a per-request timestamp the module echoes back. Functional commands are
TOGGLES: each send inverts the current state. `preset_temp` is absolute (the target
value is appended to the command). The client must therefore read status first and
only send a toggle when current != desired -- see client.py.
"""

from __future__ import annotations

import json
import time
from typing import Any

PORT = 8990

TYPE_COMMAND = 1
TYPE_STATUS = 2

# intent -> hex request prefix (before checksum).
# NOTE: status is 8888060FEE0F01. (An earlier hand-written recap mislabeled
# 8888050F0C as "status"; that hex is actually the preset_temp command.)
COMMANDS: dict[str, str] = {
    "status": "8888060FEE0F01",
    "power": "8888060F014000",
    "filter": "8888060F010004",
    "heater": "8888060F010010",
    "jets": "8888060F011000",
    "bubbles": "8888060F010400",
    "sanitizer": "8888060F010001",
    "preset_temp": "8888050F0C",
}

# boolean function fields, in status bit order (offset 104..109)
TOGGLE_FIELDS: tuple[str, ...] = (
    "power",
    "filter",
    "heater",
    "jets",
    "bubbles",
    "sanitizer",
)
_BIT_OFFSET = {name: 104 + i for i, name in enumerate(TOGGLE_FIELDS)}

# operational setpoint bounds for this model, in Celsius
TEMP_MIN_C = 20
TEMP_MAX_C = 40


class SpaProtocolError(Exception):
    """Malformed or unexpected frame from the spa."""


def checksum_int(data_hex: str) -> int:
    """Checksum byte: 0xFF minus the sum of payload bytes, mod 0xFF, 0x00->0xFF.

    The mod is 0xFF (255), not 0x100 -- this is the module's actual algorithm.
    """
    c = 0xFF
    for i in range(0, len(data_hex), 2):
        c -= int(data_hex[i : i + 2], 16)
    c %= 0xFF
    return 0xFF if c == 0 else c


def checksum_str(data_hex: str) -> str:
    """Request checksum, NOT zero-padded -- matches the module's expectation."""
    return format(checksum_int(data_hex), "X")


def _expect_status_hex(data_hex: Any) -> str:
    """Validate the full status hex payload before bit-level decoding."""
    if not isinstance(data_hex, str):
        raise SpaProtocolError(f"data is not a string: {type(data_hex).__name__}")
    if len(data_hex) != 38:
        raise SpaProtocolError(f"unexpected status length {len(data_hex)}: {data_hex!r}")
    if len(data_hex) % 2:
        raise SpaProtocolError(f"odd-length hex data: {data_hex!r}")
    try:
        int(data_hex, 16)
    except ValueError as e:
        raise SpaProtocolError(f"non-hex status data: {data_hex!r}") from e
    return data_hex


def build_request(intent: str, preset_temp: int | None = None) -> tuple[bytes, str]:
    """Frame a request. Returns (raw line bytes, sid) -- sid echoes in the reply."""
    if intent not in COMMANDS:
        raise ValueError(f"unknown intent: {intent!r}")
    req = COMMANDS[intent]
    if intent == "preset_temp":
        if preset_temp is None:
            raise ValueError("preset_temp requires a temperature")
        req = req + format(preset_temp, "X")
    sid = str(int(time.time() * 10000))
    line = json.dumps({"data": req + checksum_str(req), "sid": sid, "type": TYPE_COMMAND})
    return line.encode(), sid


def decode_status(data_hex: str) -> dict:
    """Decode a full status `data` hex string (incl. trailing checksum byte)."""
    data_hex = _expect_status_hex(data_hex)
    raw = int(data_hex, 16)
    preset = (raw >> 24) & 0xFF
    current_raw = (raw >> 88) & 0xFF
    is_error = current_raw >= 181
    status = {name: bool((raw >> off) & 1) for name, off in _BIT_OFFSET.items()}
    status.update(
        current_temp=None if is_error else current_raw,
        preset_temp=preset,
        unit="C" if preset <= 40 else "F",
        error_code=f"E{current_raw - 100}" if is_error else None,
    )
    return status


def parse_response(line: bytes, expected_sid: str | None = None) -> dict:
    """Validate a response line (checksum/sid/result) and return decoded status."""
    try:
        resp = json.loads(line.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SpaProtocolError(f"unparseable frame: {line!r}") from e

    if resp.get("result") != "ok":
        raise SpaProtocolError(f"result not ok: {resp.get('result')!r}")
    if resp.get("type") != TYPE_STATUS:
        raise SpaProtocolError(f"unexpected response type: {resp.get('type')!r}")
    if expected_sid is not None and resp.get("sid") != expected_sid:
        raise SpaProtocolError(f"sid mismatch: sent {expected_sid}, got {resp.get('sid')}")

    data_hex = _expect_status_hex(resp.get("data", ""))
    if checksum_int(data_hex[:-2]) != int(data_hex[-2:], 16):
        raise SpaProtocolError(f"checksum mismatch on {data_hex!r}")

    return decode_status(data_hex)
