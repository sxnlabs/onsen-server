"""Transactional SMS through the OVH API — dependency-free, fail-soft.

Onsen texts its owner directly instead of routing alerts through Argos: Argos
runs on the same host as this app, so it can't be what notices that host go
away, and a spa-side fault (an E90, a heat cycle that stopped climbing) is
something only Onsen can name in the message.

Auth is OVH's v1 scheme — SHA1 over
`secret + consumer_key + METHOD + url + body + timestamp`, sent as `$1$<digest>`.
The API server's clock is authoritative, so we read `/auth/time` once and keep
the offset: a host whose clock drifts would otherwise have every signature
rejected with no obvious cause.

Every network path is best-effort, same contract as weather.py: the blocking
urllib call runs in the shared I/O pool so the poll loop is never stalled, and a
failed send returns False so the caller can retry on its next tick instead of
silently dropping the alert.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .concurrency import run_blocking
from .errors import describe

_LOG = logging.getLogger("intex_spa.sms")

BASE_URL = "https://eu.api.ovh.com/1.0"
MAX_LEN = 160  # one GSM-7 part; past this OVH bills (and sends) several SMS


def alerting_env(state_path: str | Path = "state/.sms") -> dict:
    """Alerting settings: `state/.sms` (key=value), overridden by the real env.

    launchd carries no environment of its own, so on the LaunchAgent path
    anything not baked into the plist is lost — and these are credentials, which
    belong in a 0600 file rather than a world-readable plist. Same split as the
    UI password (`state/.password`, written by install.sh, kept out of the plist).
    """
    values: dict[str, str] = {}
    path = Path(state_path)
    if path.exists():
        try:
            lines = path.read_text().splitlines()
        except OSError:
            _LOG.warning("failed to read %s", state_path, exc_info=True)
            lines = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    # Not `if v`: an explicitly empty ONSEN_SMS_TO is how alerting is turned off,
    # and dropping empty overrides would leave a stale state/.sms texting on.
    values.update(os.environ)
    return values


@dataclass(frozen=True)
class OvhCredentials:
    application_key: str
    application_secret: str
    consumer_key: str
    service_name: str
    sender: str = "SXNLABS"

    @classmethod
    def from_env(cls, env: dict | None = None) -> OvhCredentials | None:
        """Build from the environment, or None when alerting isn't configured.

        Missing credentials are not an error: SMS is opt-in, exactly like the
        camera and the password gate. The caller degrades to no alerting.
        """
        env = os.environ if env is None else env
        required = (
            "OVH_APPLICATION_KEY",
            "OVH_APPLICATION_SECRET",
            "OVH_CONSUMER_KEY",
            "OVH_SMS_SERVICE",
        )
        values = [(env.get(name) or "").strip() for name in required]
        if not all(values):
            return None
        return cls(*values, sender=(env.get("OVH_SMS_SENDER") or "SXNLABS").strip())


# GSM 03.38 basic set + extension table. Anything outside it forces OVH into
# UCS-2, which halves the payload to 70 chars — a French alert would silently
# become two SMS, or get truncated mid-word.
_GSM7 = set(
    "@£$¥èéùìòÇØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà\n\r"
)
_GSM7_EXT = set("^{}\\[~]|€")
# Characters a French alert reaches for that GSM-7 lacks, with the substitution
# a reader won't stumble over. Everything else falls back to NFKD + ASCII.
_FOLD = {
    "ê": "e", "ë": "e", "â": "a", "î": "i", "ï": "i", "ô": "o", "û": "u", "ü": "u",
    "ç": "c", "œ": "oe", "Œ": "OE", "’": "'", "‘": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", "°": "o", " ": " ", " ": " ",
}


def gsm7_safe(text: str) -> str:
    """Fold `text` into the GSM-7 alphabet, losing accents rather than characters."""
    out: list[str] = []
    for ch in text:
        if ch in _GSM7 or ch in _GSM7_EXT:
            out.append(ch)
            continue
        folded = _FOLD.get(ch)
        if folded is None:
            folded = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
        out.append(folded or "?")
    return "".join(out)


class SmsSender:
    """Sends one short message to one recipient. Never raises."""

    def __init__(
        self,
        credentials: OvhCredentials,
        recipient: str,
        *,
        timeout: float = 10.0,
        base_url: str = BASE_URL,
    ) -> None:
        self.credentials = credentials
        self.recipient = recipient
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._clock_offset: float | None = None

    async def send(self, message: str) -> bool:
        return await run_blocking(self.send_blocking, message)

    def send_blocking(self, message: str) -> bool:
        body = gsm7_safe(message)[:MAX_LEN]
        payload = {
            "message": body,
            "receivers": [self.recipient],
            "sender": self.credentials.sender,
            "noStopClause": True,   # transactional alert, not marketing
            "priority": "high",
            "coding": "7bit",
            "charset": "UTF-8",
        }
        try:
            result = self._post(f"/sms/{self.credentials.service_name}/jobs", payload)
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
            _LOG.warning("SMS send failed: %s", describe(e))
            return False

        invalid = result.get("invalidReceivers") or []
        if invalid:
            _LOG.error("SMS refused: invalid receiver %s", self._masked())
            return False
        if not result.get("ids"):
            _LOG.error("SMS refused by OVH (no job id): %s", result)
            return False
        _LOG.info(
            "SMS sent to %s (%s credit(s), %d chars)",
            self._masked(), result.get("totalCreditsRemoved"), len(body),
        )
        return True

    # -- internals ------------------------------------------------------------
    def _masked(self) -> str:
        """Last two digits only — alert logs end up in `docker logs`."""
        return f"...{self.recipient[-2:]}" if len(self.recipient) > 2 else "..."

    def _timestamp(self) -> str:
        """OVH server time. Fetched once, then tracked against the local clock."""
        if self._clock_offset is None:
            req = urllib.request.Request(
                f"{self.base_url}/auth/time", headers={"User-Agent": "onsen/1.0"}
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 (https only)
                server_now = int(r.read().decode().strip())
            self._clock_offset = server_now - time.time()
        return str(int(time.time() + self._clock_offset))

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        body = json.dumps(payload)
        timestamp = self._timestamp()
        raw = "+".join(
            (
                self.credentials.application_secret,
                self.credentials.consumer_key,
                "POST",
                url,
                body,
                timestamp,
            )
        )
        req = urllib.request.Request(
            url,
            data=body.encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "onsen/1.0",
                "X-Ovh-Application": self.credentials.application_key,
                "X-Ovh-Consumer": self.credentials.consumer_key,
                "X-Ovh-Timestamp": timestamp,
                "X-Ovh-Signature": "$1$" + hashlib.sha1(raw.encode()).hexdigest(),  # noqa: S324 (OVH v1 scheme)
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 (https only)
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # OVH puts the actual reason in the body ("Invalid signature",
            # "This service does not exist"); without it the caller only sees 403.
            detail = e.read().decode(errors="replace")[:200]
            raise urllib.error.URLError(f"HTTP {e.code}: {detail}") from e
