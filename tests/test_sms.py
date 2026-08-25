"""SMS plumbing: credentials from env, GSM-7 folding, and never-empty errors."""

from __future__ import annotations


from intex_spa.errors import describe
from intex_spa.sms import MAX_LEN, OvhCredentials, SmsSender, gsm7_safe

ENV = {
    "OVH_APPLICATION_KEY": "ak",
    "OVH_APPLICATION_SECRET": "as",
    "OVH_CONSUMER_KEY": "ck",
    "OVH_SMS_SERVICE": "sms-xx-1",
}


def test_credentials_need_every_key():
    assert OvhCredentials.from_env(ENV) is not None
    for missing in ENV:
        partial = {k: v for k, v in ENV.items() if k != missing}
        assert OvhCredentials.from_env(partial) is None


def test_credentials_default_the_sender():
    assert OvhCredentials.from_env(ENV).sender == "SXNLABS"
    assert OvhCredentials.from_env({**ENV, "OVH_SMS_SENDER": "ONSEN"}).sender == "ONSEN"


def test_gsm7_keeps_the_accents_the_alphabet_has():
    # è/é/à are in GSM 03.38; ê and û are not and must fold, not vanish.
    assert gsm7_safe("température à 36C") == "température à 36C"
    assert gsm7_safe("arrêt de la chauffe") == "arret de la chauffe"
    assert gsm7_safe("œuf — 30°C…") == "oeuf - 30oC..."


def test_message_is_truncated_to_one_part(monkeypatch):
    """Past 160 GSM-7 chars OVH sends (and bills) several SMS."""
    sent: dict = {}
    sender = SmsSender(OvhCredentials.from_env(ENV), "+33600000000")
    monkeypatch.setattr(sender, "_post", lambda path, payload: sent.update(payload) or {"ids": [1]})

    assert sender.send_blocking("x" * 500) is True
    assert len(sent["message"]) == MAX_LEN
    assert sent["receivers"] == ["+33600000000"]


def test_send_is_false_when_ovh_refuses(monkeypatch):
    sender = SmsSender(OvhCredentials.from_env(ENV), "+33600000000")
    monkeypatch.setattr(sender, "_post", lambda path, payload: {"invalidReceivers": ["+336"]})
    assert sender.send_blocking("hello") is False

    monkeypatch.setattr(sender, "_post", lambda path, payload: {"ids": []})
    assert sender.send_blocking("hello") is False


def test_send_is_false_when_the_network_fails(monkeypatch):
    def boom(path, payload):
        raise OSError()

    sender = SmsSender(OvhCredentials.from_env(ENV), "+33600000000")
    monkeypatch.setattr(sender, "_post", boom)
    assert sender.send_blocking("hello") is False


async def test_send_awaits_without_blocking_the_loop(monkeypatch):
    sender = SmsSender(OvhCredentials.from_env(ENV), "+33600000000")
    monkeypatch.setattr(sender, "_post", lambda path, payload: {"ids": [1]})
    assert await sender.send("hello") is True


def test_describe_never_returns_an_empty_string():
    """The 46h outage logged `network error (attempt 1): ` — this is why."""
    assert describe(TimeoutError()) == "TimeoutError"
    assert describe(ConnectionResetError()) == "ConnectionResetError"
    assert describe(ValueError("boom")) == "ValueError: boom"


def test_alerting_env_reads_the_state_file_and_the_env_wins(tmp_path, monkeypatch):
    """launchd carries no env: state/.sms is where the LaunchAgent path finds these."""
    from intex_spa.sms import alerting_env as _alerting_env

    state = tmp_path / ".sms"
    state.write_text(
        "# written by install.sh\n"
        "ONSEN_SMS_TO=+33600000000\n"
        'OVH_APPLICATION_KEY="ak"\n'
        "\n"
        "OVH_SMS_SERVICE=sms-file-1\n"
    )
    monkeypatch.setenv("OVH_SMS_SERVICE", "sms-env-1")

    values = _alerting_env(str(state))
    assert values["ONSEN_SMS_TO"] == "+33600000000"
    assert values["OVH_APPLICATION_KEY"] == "ak"   # quotes stripped
    assert values["OVH_SMS_SERVICE"] == "sms-env-1"  # the real env overrides the file


def test_alerting_env_is_empty_without_a_state_file(tmp_path, monkeypatch):
    from intex_spa.sms import alerting_env as _alerting_env

    for name in (*ENV, "ONSEN_SMS_TO"):
        monkeypatch.delenv(name, raising=False)
    assert OvhCredentials.from_env(_alerting_env(str(tmp_path / "nope"))) is None


def test_an_empty_env_value_disables_a_persisted_recipient(tmp_path, monkeypatch):
    """`ONSEN_SMS_TO=` is the documented off switch — a stale state/.sms must not
    keep texting through it."""
    from intex_spa.sms import alerting_env as _alerting_env

    state = tmp_path / ".sms"
    state.write_text("ONSEN_SMS_TO=+33600000000\n")
    monkeypatch.setenv("ONSEN_SMS_TO", "")

    assert _alerting_env(str(state))["ONSEN_SMS_TO"] == ""
