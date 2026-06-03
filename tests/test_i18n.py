"""Unit tests for the i18n module + HTTP-level lang detection.

The translation bundles are real (loaded from web/translations/*.json), so these
also catch typos in either bundle that would silently fall back to EN.
"""

from contextlib import asynccontextmanager

import httpx
import pytest

from fake_spa import FakeSpa
from web import i18n
from web.main import create_app


# -- pure module tests ------------------------------------------------------
def test_supported_langs_are_loaded():
    # both bundles exist and have at least one entry each
    assert "en" in i18n.LANGS and "fr" in i18n.LANGS
    assert i18n.bundle("en")
    assert i18n.bundle("fr")


def test_t_returns_translation_for_lang():
    assert i18n.t("en", "sched.title") == "Schedule"
    assert i18n.t("fr", "sched.title") == "Programmation"


def test_t_falls_back_to_en_then_key():
    # unknown lang → falls back to EN bundle
    assert i18n.t("xx", "sched.title") == "Schedule"
    # unknown key → returns the key itself (visible signal in the UI)
    assert i18n.t("en", "this.key.does.not.exist") == "this.key.does.not.exist"


def test_t_substitutes_named_placeholders():
    # FR retains the placeholder shape — same {value} stays substitutable
    fr = i18n.t("fr", "weather.feels", value=12.3)
    assert fr == "ressenti 12.3°"


def test_bundle_merges_en_under_lang():
    # Every key reachable in EN must be reachable in the merged FR bundle,
    # even if FR is missing a translation (the merge resolves to the EN string).
    en = i18n.bundle("en")
    fr = i18n.bundle("fr")
    for key in en:
        assert key in fr, f"missing in merged FR bundle: {key}"


def test_detect_priority_cookie_over_header():
    # cookie wins
    assert i18n.detect("fr", "en-US") == "fr"
    assert i18n.detect("en", "fr-FR,fr;q=0.9") == "en"
    # cookie absent → header
    assert i18n.detect(None, "fr-FR,fr;q=0.9") == "fr"
    assert i18n.detect(None, "en-US,en;q=0.9") == "en"
    # unsupported cookie → fall back to header
    assert i18n.detect("zh", "fr") == "fr"
    # nothing usable → default
    assert i18n.detect(None, None) == i18n.DEFAULT_LANG
    assert i18n.detect(None, "es-MX,es;q=0.9") == i18n.DEFAULT_LANG


# -- HTTP-level lang propagation --------------------------------------------
@asynccontextmanager
async def _client(spa, **kw):
    host, port = await spa.start()
    kw.setdefault("weather_enabled", False)
    kw.setdefault("camera_config_path", None)
    kw.setdefault("manual_override_path", None)
    kw.setdefault("pause_path", None)
    app = create_app(host, port=port, poll_interval=9999,
                     history_path=None, schedule_path=None, **kw)
    await app.state.supervisor.refresh()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c
    finally:
        await app.state.supervisor.client.close()
        await spa.stop()


async def test_index_renders_en_by_default():
    spa = FakeSpa()
    async with _client(spa) as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "Schedule" in r.text
        assert 'lang="en"' in r.text


async def test_index_renders_fr_via_accept_language():
    spa = FakeSpa()
    async with _client(spa) as c:
        r = await c.get("/", headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
        assert "Programmation" in r.text
        assert 'lang="fr"' in r.text


async def test_lang_query_param_redirects_and_sets_cookie():
    spa = FakeSpa()
    async with _client(spa) as c:
        # ?lang=fr → 303 to bare path, Set-Cookie lang=fr
        r = await c.get("/?lang=fr")
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        assert "lang=fr" in r.headers.get("set-cookie", "")


async def test_lang_cookie_overrides_accept_language():
    spa = FakeSpa()
    async with _client(spa) as c:
        # cookie wins even if Accept-Language says otherwise
        r = await c.get(
            "/",
            cookies={"lang": "fr"},
            headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        assert "Programmation" in r.text


async def test_invalid_lang_query_falls_through_to_render():
    spa = FakeSpa()
    async with _client(spa) as c:
        # unknown lang param is ignored, page renders normally (no redirect)
        r = await c.get("/?lang=zz", follow_redirects=False)
        assert r.status_code == 200


async def test_i18n_bundle_is_injected_for_js():
    spa = FakeSpa()
    async with _client(spa) as c:
        r = await c.get("/")
        assert "window.I18N" in r.text
        # the bundle JSON should contain at least one known key
        assert "sched.title" in r.text
