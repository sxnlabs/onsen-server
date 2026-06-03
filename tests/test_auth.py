"""Auth tests: the password gate protects the UI but leaves /static + /healthz open."""

from contextlib import asynccontextmanager

import httpx

from fake_spa import FakeSpa
from web.main import create_app

PW = "hunter2"


@asynccontextmanager
async def auth_client(spa: FakeSpa, tmp_path, password: str | None = PW):
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
