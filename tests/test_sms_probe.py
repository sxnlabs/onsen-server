"""The prod smoke test itself: it must not lie about being armed."""

from __future__ import annotations

import sms_probe

CONFIGURED = (
    "ONSEN_SMS_TO=+33600000000\n"
    "OVH_APPLICATION_KEY=ak\n"
    "OVH_APPLICATION_SECRET=as\n"
    "OVH_CONSUMER_KEY=ck\n"
    "OVH_SMS_SERVICE=sms-xx-1\n"
)


def clean_env(monkeypatch):
    for name in (
        "ONSEN_SMS_TO",
        "OVH_APPLICATION_KEY",
        "OVH_APPLICATION_SECRET",
        "OVH_CONSUMER_KEY",
        "OVH_SMS_SERVICE",
        "OVH_SMS_SENDER",
    ):
        monkeypatch.delenv(name, raising=False)


def state_file(tmp_path, content=CONFIGURED):
    path = tmp_path / ".sms"
    path.write_text(content)
    return str(path)


def test_unconfigured_exits_one(tmp_path, monkeypatch, capsys):
    clean_env(monkeypatch)
    assert sms_probe.main(["--state", str(tmp_path / "nope")]) == 1
    assert "not configured" in capsys.readouterr().err


def test_half_configured_names_the_missing_half(tmp_path, monkeypatch, capsys):
    clean_env(monkeypatch)
    path = state_file(tmp_path, "ONSEN_SMS_TO=+33600000000\n")
    assert sms_probe.main(["--state", path]) == 1
    assert "OVH_APPLICATION_KEY" in capsys.readouterr().err


def test_dry_run_sends_nothing(tmp_path, monkeypatch, capsys):
    clean_env(monkeypatch)
    monkeypatch.setattr(
        sms_probe.SmsSender,
        "send_blocking",
        lambda self, message: pytest_fail("dry run must not send"),
    )
    assert sms_probe.main(["--state", state_file(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "...0000" in out and "nothing sent" in out


def test_a_refused_send_exits_two(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    monkeypatch.setattr(sms_probe.SmsSender, "send_blocking", lambda self, message: False)
    assert sms_probe.main(["--state", state_file(tmp_path)]) == 2


def test_a_sent_message_exits_zero(tmp_path, monkeypatch, capsys):
    clean_env(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        sms_probe.SmsSender,
        "send_blocking",
        lambda self, message: seen.update(message=message, to=self.recipient) or True,
    )
    assert sms_probe.main(["--state", state_file(tmp_path)]) == 0
    assert seen["to"] == "+33600000000"
    assert "surveillance" in seen["message"].lower()
    assert "sent" in capsys.readouterr().out


def pytest_fail(reason: str):
    raise AssertionError(reason)
