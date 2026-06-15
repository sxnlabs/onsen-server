"""Auth tests: the password gate protects the UI but leaves /static + /healthz open."""

from contextlib import asynccontextmanager

import httpx

from fake_spa import FakeSpa
from web import auth
from web.main import create_app

PW = "hunter2"


@asynccontextmanager
async def auth_client(spa: FakeSpa, tmp_path, password: str | None = PW, login_limiter=None):
    host, port = await spa.start()
    app = create_app(
        host,
        port=port,
        poll_interval=9999,
        history_path=None,
        schedule_path=None,
        command_log_path=None,
        manual_override_path=None,
        pause_path=None,
        automation_cooldown_path=None,
        login_limiter=login_limiter,
        password=password,
        secret_path=str(tmp_path / ".secret"),
    )
    await app.state.supervisor.refresh()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t", follow_redirects=False
        ) as client:
            yield client
    finally:
        await app.state.supervisor.client.close()
        await spa.stop()


async def test_unauthed_get_redirects_to_login(tmp_path):
    spa = FakeSpa()
    async with auth_client(spa, tmp_path) as client:
        r = await client.get("/")
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


async def test_unauthed_post_is_401(tmp_path):
    spa = FakeSpa()
    async with auth_client(spa, tmp_path) as client:
        r = await client.post("/toggle/bubbles")
        assert r.status_code == 401
        assert spa.state["bubbles"] is False  # command never reached the spa


async def test_login_page_and_public_paths(tmp_path):
    spa = FakeSpa()
    async with auth_client(spa, tmp_path) as client:
        assert (await client.get("/login")).status_code == 200
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/static/app.css")).status_code == 200


async def test_wrong_password_rejected(tmp_path):
    spa = FakeSpa()
    async with auth_client(spa, tmp_path) as client:
        r = await client.post("/login", data={"password": "nope"})
        assert r.status_code == 401


async def test_login_then_access_granted(tmp_path):
    spa = FakeSpa()
    async with auth_client(spa, tmp_path) as client:
        r = await client.post("/login", data={"password": PW})
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        assert client.cookies.get("spa_session")  # cookie set
        # cookie jar carries it forward
        r2 = await client.get("/")
        assert r2.status_code == 200
        r3 = await client.post("/toggle/bubbles")
        assert r3.status_code == 200
        assert spa.state["bubbles"] is True


async def test_no_password_means_no_gate(tmp_path):
    spa = FakeSpa()
    async with auth_client(spa, tmp_path, password=None) as client:
        assert (await client.get("/")).status_code == 200


# -- hardening for the internet-exposed UI -----------------------------------
def test_login_throttle_locks_window_and_reset():
    t = auth.LoginThrottle(max_failures=3, window=100, lockout=50)
    assert t.retry_after("ip", now=0) == 0
    t.record_failure("ip", now=0)
    t.record_failure("ip", now=1)
    assert t.retry_after("ip", now=2) == 0          # 2 < 3 failures
    t.record_failure("ip", now=2)                    # 3rd ⇒ locked
    assert t.retry_after("ip", now=3) > 0
    assert t.retry_after("ip", now=60) == 0          # lockout expired

    # failures older than the window don't accumulate toward a lock
    w = auth.LoginThrottle(max_failures=2, window=10, lockout=5)
    w.record_failure("ip", now=0)
    w.record_failure("ip", now=20)
    assert w.retry_after("ip", now=21) == 0

    # a successful login clears the record
    r = auth.LoginThrottle(max_failures=1, window=10, lockout=5)
    r.record_failure("ip", now=0)
    assert r.retry_after("ip", now=1) > 0
    r.reset("ip")
    assert r.retry_after("ip", now=1) == 0


async def test_login_rate_limited_after_repeated_failures(tmp_path):
    spa = FakeSpa()
    async with auth_client(spa, tmp_path) as client:
        for _ in range(5):
            assert (await client.post("/login", data={"password": "nope"})).status_code == 401
        # locked out now — even the *correct* password is refused
        r = await client.post("/login", data={"password": "nope"})
        assert r.status_code == 429
        assert r.headers.get("retry-after")
        assert (await client.post("/login", data={"password": PW})).status_code == 429


async def test_security_headers_present(tmp_path):
    spa = FakeSpa()
    async with auth_client(spa, tmp_path) as client:
        r = await client.get("/login")
        assert r.headers.get("strict-transport-security", "").startswith("max-age=")
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("x-frame-options") == "DENY"
        assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")
        assert r.headers.get("referrer-policy") == "no-referrer"


def test_global_rate_limiter_token_bucket():
    rl = auth.GlobalRateLimiter(rate_per_sec=1.0, burst=3)
    assert rl.take(now=0) == 0
    assert rl.take(now=0) == 0
    assert rl.take(now=0) == 0       # burst exhausted
    assert rl.take(now=0) > 0        # blocked → returns seconds to wait
    assert rl.take(now=10) == 0      # refilled after enough time


async def test_login_global_cap_not_bypassed_by_ip_rotation(tmp_path):
    # The whole point of the global limiter: rotating X-Forwarded-For defeats the
    # per-IP lockout, but NOT the process-wide token bucket.
    spa = FakeSpa()
    limiter = auth.GlobalRateLimiter(rate_per_sec=0.001, burst=2)
    async with auth_client(spa, tmp_path, login_limiter=limiter) as client:
        for i in range(2):  # burst of 2, each from a fresh IP
            r = await client.post(
                "/login", data={"password": "nope"},
                headers={"X-Forwarded-For": f"10.0.0.{i}"},
            )
            assert r.status_code == 401
        # a third fresh IP is still blocked by the global cap
        r = await client.post(
            "/login", data={"password": "nope"},
            headers={"X-Forwarded-For": "10.0.0.99"},
        )
        assert r.status_code == 429
        assert r.headers.get("retry-after")


async def test_locked_ip_does_not_starve_global_bucket(tmp_path):
    # A per-IP-locked client must not keep consuming global tokens, or it would
    # 429 logins from every other IP. Burst 6: 5 used while locking one IP, 1 must
    # remain for a different IP.
    spa = FakeSpa()
    limiter = auth.GlobalRateLimiter(rate_per_sec=0.0001, burst=6)
    async with auth_client(spa, tmp_path, login_limiter=limiter) as client:
        locked = {"X-Forwarded-For": "10.0.0.1"}
        for _ in range(5):  # 5 failures → locks 10.0.0.1 (consumes 5 tokens)
            assert (await client.post("/login", data={"password": "nope"}, headers=locked)).status_code == 401
        for _ in range(10):  # locked: 429 via per-IP, must NOT touch the bucket
            assert (await client.post("/login", data={"password": "nope"}, headers=locked)).status_code == 429
        # a different IP still has a token left → reaches verification, not starved
        r = await client.post("/login", data={"password": "nope"}, headers={"X-Forwarded-For": "10.0.0.2"})
        assert r.status_code == 401


async def test_session_cookie_secure_only_behind_https(tmp_path):
    spa = FakeSpa()
    async with auth_client(spa, tmp_path) as client:
        # plain HTTP (e.g. the SSH-tunnel dev path) ⇒ no Secure flag, cookie still works
        r = await client.post("/login", data={"password": PW})
        assert "spa_session=" in r.headers.get("set-cookie", "")
        assert "Secure" not in r.headers.get("set-cookie", "")
        # behind the TLS-terminating LB (X-Forwarded-Proto: https) ⇒ Secure flag
        r2 = await client.post(
            "/login", data={"password": PW}, headers={"X-Forwarded-Proto": "https"}
        )
        assert "Secure" in r2.headers.get("set-cookie", "")
