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
