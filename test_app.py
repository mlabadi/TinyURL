import pytest

from app import create_app, validate_target


@pytest.fixture
def client(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "t.db"), "API_KEY": "secret"})
    return app.test_client()


H = {"X-API-Key": "secret"}


def shorten(client, **body):
    return client.post("/api/shorten", json=body, headers=H)


def test_shorten_and_redirect(client):
    r = shorten(client, url="https://www.rta.ae/wps/portal/rta/ae/home")
    assert r.status_code == 201
    j = r.get_json()
    assert j["short_url"].startswith("https://m.rta.ae/")
    assert len(j["code"]) == 7

    r = client.get(f"/{j['code']}")
    assert r.status_code == 302
    assert r.headers["Location"] == "https://www.rta.ae/wps/portal/rta/ae/home"
    assert client.get(f"/api/links/{j['code']}", headers=H).get_json()["clicks"] == 1


def test_same_url_reuses_code(client):
    a = shorten(client, url="https://rta.ae/a").get_json()["code"]
    b = shorten(client, url="https://rta.ae/a").get_json()["code"]
    assert a == b


def test_custom_alias(client):
    r = shorten(client, url="https://rta.ae/nol", alias="nol-card")
    assert r.get_json()["short_url"] == "https://m.rta.ae/nol-card"
    assert shorten(client, url="https://rta.ae/x", alias="nol-card").status_code == 409
    assert shorten(client, url="https://rta.ae/x", alias="api").status_code == 400
    assert shorten(client, url="https://rta.ae/x", alias="a b").status_code == 400


def test_api_key_required(client):
    assert client.post("/api/shorten", json={"url": "https://rta.ae"}).status_code == 401


def test_rejects_disallowed_domain(client):
    r = shorten(client, url="https://evil.example.com/login")
    assert r.status_code == 400
    assert "not allowed" in r.get_json()["error"]


def test_expired_link_returns_410(client):
    code = shorten(client, url="https://rta.ae/e", expires_in_days=1).get_json()["code"]
    import sqlite3
    db = sqlite3.connect(client.application.config["DATABASE"])
    db.execute("UPDATE links SET expires_at='2000-01-01T00:00:00+00:00' WHERE code=?", (code,))
    db.commit()
    db.close()
    assert client.get(f"/{code}").status_code == 410


def test_delete_and_unknown(client):
    code = shorten(client, url="https://rta.ae/d").get_json()["code"]
    assert client.delete(f"/api/links/{code}", headers=H).status_code == 204
    assert client.get(f"/{code}").status_code == 404


@pytest.mark.parametrize("url,ok", [
    ("https://rta.ae", True),
    ("https://www.rta.ae/path?q=1", True),
    ("http://sub.rta.ae", True),
    ("https://notrta.ae", False),          # suffix trick
    ("https://rta.ae.evil.com", False),    # prefix trick
    ("https://user:pw@rta.ae", False),     # embedded credentials
    ("javascript:alert(1)", False),
    ("ftp://rta.ae", False),
    ("", False),
])
def test_validate_target(url, ok):
    assert (validate_target(url, ["rta.ae"]) is None) == ok


def test_wildcard_allows_any_domain():
    assert validate_target("https://example.com", ["*"]) is None
