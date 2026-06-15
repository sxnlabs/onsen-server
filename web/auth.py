"""Optional single-password auth — dependency-free signed-cookie sessions.

Off unless a password is configured (HERMES_PASSWORD). This protects the *web UI*
only; it does nothing for the spa's own unauthenticated TCP port — lock that down at
the firewall. See README.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

COOKIE_NAME = "spa_session"
DEFAULT_MAX_AGE = 14 * 24 * 3600  # 14 days


def load_or_create_secret(path: str | Path) -> bytes:
    """Persist a random HMAC secret so cookies survive restarts."""
    p = Path(path)
    if p.exists() and p.read_bytes():
        return p.read_bytes()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(32)
    p.write_bytes(data)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return data


def issue_token(secret: bytes, now: float | None = None) -> str:
    ts = str(int(time.time() if now is None else now))
    sig = hmac.new(secret, ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def token_valid(
    token: str | None, secret: bytes, max_age: int = DEFAULT_MAX_AGE, now: float | None = None
) -> bool:
    if not token:
        return False
    try:
        ts_s, sig = token.split(".", 1)
        expected = hmac.new(secret, ts_s.encode(), hashlib.sha256).hexdigest()
    except ValueError:
        return False
    if not hmac.compare_digest(sig, expected):
        return False
    age = (time.time() if now is None else now) - int(ts_s)
    return 0 <= age <= max_age


def password_ok(supplied: str | None, expected: str) -> bool:
    return hmac.compare_digest((supplied or "").encode(), (expected or "").encode())


class LoginThrottle:
    """In-memory per-client failed-login limiter (single-process app).

    After `max_failures` failures within `window` seconds, a client is locked
    out for `lockout` seconds (`retry_after` then returns the remaining time).
    A successful login clears the client's record.

    Best-effort: the client key is the forwarded IP, which a determined attacker
    can rotate — so the caller also imposes a fixed per-attempt delay on failure,
    bounding total throughput regardless of source IP. Together with a
    high-entropy password this makes online guessing infeasible.
    """

    def __init__(self, max_failures: int = 5, window: float = 900.0, lockout: float = 900.0) -> None:
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self._fails: dict[str, list[float]] = {}
        self._until: dict[str, float] = {}

    def retry_after(self, key: str, now: float | None = None) -> int:
        """Seconds the client must wait, or 0 if not locked out."""
        now = time.time() if now is None else now
        until = self._until.get(key, 0.0)
        return int(until - now) + 1 if until > now else 0

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        recent = [t for t in self._fails.get(key, []) if now - t < self.window]
        recent.append(now)
        if len(recent) >= self.max_failures:
            self._until[key] = now + self.lockout
            self._fails.pop(key, None)
        else:
            self._fails[key] = recent

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)
        self._until.pop(key, None)
