"""Offline protocol tests — checksum, request framing, status decode.

Gold vectors are from mathieu-mp/aio-intex-spa plus one frame captured from the
real device on 2026-05-22.
"""

import json

import pytest

from intex_spa import protocol


def test_request_checksums():
    assert protocol.checksum_str("8888060FEE0F01") == "DA"  # status request
    assert protocol.checksum_str("8888060F010004") == "D4"  # filter toggle


def test_checksum_zero_maps_to_ff():
    # 36 zero nibbles -> 0xFF - 0 = 0xFF, %0xFF = 0 -> remapped to 0xFF
    assert protocol.checksum_int("0" * 36) == 0xFF


def test_build_request_status():
    line, sid = protocol.build_request("status")
    d = json.loads(line)
    assert d["data"] == "8888060FEE0F01DA"
    assert d["type"] == protocol.TYPE_COMMAND
    assert d["sid"] == sid and sid.isdigit()


def test_build_request_preset_is_absolute():
    line, _ = protocol.build_request("preset_temp", 37)  # 37 -> 0x25
    d = json.loads(line)
    prefix = "8888050F0C25"
    assert d["data"] == prefix + protocol.checksum_str(prefix)


def test_build_request_preset_requires_temp():
    with pytest.raises(ValueError):
        protocol.build_request("preset_temp")


def test_decode_standard_frame():
    s = protocol.decode_status("FFFF110F010700220000000080808022000012")
    assert s["power"] and s["filter"] and s["heater"]
    assert not s["jets"] and not s["bubbles"] and not s["sanitizer"]
    assert s["current_temp"] == 34
    assert s["preset_temp"] == 34
    assert s["unit"] == "C"
    assert s["error_code"] is None


def test_decode_error_frame():
    s = protocol.decode_status("FFFF110F010700B50000000080808022000012")
    assert s["current_temp"] is None
    assert s["error_code"] == "E81"
    assert s["preset_temp"] == 34


def test_decode_real_device_frame():
    # captured live from the Baltik at 192.168.20.189 on 2026-05-22
    s = protocol.decode_status("FFFF110F010100130000000080808025000024")
    assert s["power"] is True
    assert s["filter"] is False and s["heater"] is False
    assert s["current_temp"] == 19
    assert s["preset_temp"] == 37
    assert s["unit"] == "C"


def test_parse_response_valid():
    line = b'{"sid":"12345678901234","data":"FFFF110F010700220000000080808022000012","result":"ok","type":2}\n'
    s = protocol.parse_response(line, expected_sid="12345678901234")
    assert s["current_temp"] == 34


def test_parse_response_bad_checksum():
    line = b'{"sid":"x","data":"FFFF110F010700220000000080808022000044","result":"ok","type":2}\n'
    with pytest.raises(protocol.SpaProtocolError):
        protocol.parse_response(line)


def test_parse_response_sid_mismatch():
    line = b'{"sid":"theirs","data":"FFFF110F010700220000000080808022000012","result":"ok","type":2}\n'
    with pytest.raises(protocol.SpaProtocolError):
        protocol.parse_response(line, expected_sid="mine")


def test_parse_response_not_ok():
    line = b'{"sid":"x","data":"FFFF110F010700220000000080808022000012","result":"error","type":2}\n'
    with pytest.raises(protocol.SpaProtocolError):
        protocol.parse_response(line)


def test_decode_rejects_short_status_frame():
    with pytest.raises(protocol.SpaProtocolError):
        protocol.decode_status("FF")


def test_parse_response_rejects_non_status_type():
    line = b'{"sid":"x","data":"FFFF110F010700220000000080808022000012","result":"ok","type":1}\n'
    with pytest.raises(protocol.SpaProtocolError):
        protocol.parse_response(line)


def test_parse_response_rejects_non_hex_data():
    line = b'{"sid":"x","data":"FFFF110F0107002200000000808080220000ZZ","result":"ok","type":2}\n'
    with pytest.raises(protocol.SpaProtocolError):
        protocol.parse_response(line)
