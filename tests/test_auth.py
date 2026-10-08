from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db
from app.web import auth

PASSWORD = "correct horse battery"


# ---------- building blocks ----------

def test_password_hash_roundtrip():
    stored = auth.hash_password(PASSWORD, iterations=1000)
    assert PASSWORD not in stored
    assert auth.verify_password(PASSWORD, stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.verify_password(PASSWORD, "garbage")
    assert auth.hash_password(PASSWORD, 1000) != auth.hash_password(PASSWORD, 1000)  # salted


def test_session_cookie_signing_and_expiry():
    cookie = auth.make_session("admin", "sid1", "s3cret", max_age=60, now=1000)
    assert auth.read_session(cookie, "s3cret", now=1030) == ("admin", "sid1")
    assert auth.read_session(cookie, "s3cret", now=1061) is None          # expired
    assert auth.read_session(cookie, "other", now=1030) is None           # wrong secret
    user, sid, expires, sig = cookie.split("|")
    assert auth.read_session(f"{user}|{sid}|{int(expires) + 9999}|{sig}", "s3cret", now=1030) is None  # extended
    assert auth.read_session(f"root|{sid}|{expires}|{sig}", "s3cret", now=1030) is None                # renamed
    assert auth.read_session(f"{user}|sid2|{expires}|{sig}", "s3cret", now=1030) is None               # other session
    assert auth.read_session(None, "s3cret") is None


def test_server_side_sessions(tmp_path):
    path = tmp_path / "s.db"
    db.init_db(path)
    with db.session(path) as conn:
        a = auth.start_session(conn, 60, now=1000)
        b = auth.start_session(conn, 60, now=1000)
        assert auth.session_active(conn, a, now=1030) and auth.session_active(conn, b, now=1030)
        auth.end_session(conn, a)
        assert not auth.session_active(conn, a, now=1030) and auth.session_active(conn, b, now=1030)
        assert not auth.session_active(conn, b, now=1061)  # expired
        auth.end_all_sessions(conn)
        assert not auth.session_active(conn, b, now=1030)


def test_limiter_locks_after_five_failures():
    lim = auth.LoginLimiter(max_failures=5, lock_seconds=900)
    for _ in range(4):
        lim.failed("1.2.3.4", now=0)
    assert lim.seconds_locked("1.2.3.4", now=0) == 0
    lim.failed("1.2.3.4", now=0)
    assert lim.seconds_locked("1.2.3.4", now=10) == 890
    assert lim.seconds_locked("5.6.7.8", now=10) == 0
    assert lim.seconds_locked("1.2.3.4", now=901) == 0


@pytest.mark.parametrize("target,expected", [("/status", "/status"), ("/analytics?src=backtest", "/analytics?src=backtest"),
                                             ("https://evil.example", "/"), ("//evil.example", "/"),
                                             ("/\\evil.example", "/"), (None, "/")])
def test_safe_next(target, expected):
    assert auth.safe_next(target) == expected


# ---------- whole site ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.web import main

    path = tmp_path / "web.db"
    db.init_db(path)
    monkeypatch.setattr(db, "settings", SimpleNamespace(database_path=path))
    monkeypatch.setattr(main, "settings", SimpleNamespace(**{
        **vars(main.settings), "database_path": path, "admin_username": "admin",
        "admin_password_hash": auth.hash_password(PASSWORD, iterations=1000), "session_secret": "test-secret",
        "site_url": "https://example.test"}))
    monkeypatch.setattr(main, "limiter", auth.LoginLimiter())
    with TestClient(main.app, base_url="https://testserver") as c:
        yield c


@pytest.mark.parametrize("path", ["/", "/status", "/analytics", "/chart", "/performance", "/api/signals"])
def test_every_page_needs_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


def test_open_paths_stay_open(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.post("/logout", follow_redirects=False).status_code == 401  # nothing to sign out of


@pytest.mark.parametrize("path", ["/pricing", "/checkout", "/success", "/stripe/webhook", "/disclaimer"])
def test_payment_pages_are_gone(client, path):
    client.post("/login", data={"username": "admin", "password": PASSWORD})
    assert client.get(path).status_code == 404


def test_wrong_password_then_right_password(client):
    r = client.post("/login", data={"username": "admin", "password": "nope", "next": "/status"})
    assert r.status_code == 401 and "Wrong username or password" in r.text
    assert client.get("/status", follow_redirects=False).status_code == 303

    r = client.post("/login", data={"username": "Admin", "password": PASSWORD, "next": "/status", "remember": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/status"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie and "Max-Age=2592000" in cookie
    assert client.get("/status").status_code == 200
    assert "Sign out" in client.get("/analytics").text

    stolen = client.cookies.get(auth.COOKIE)
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    client.cookies.clear()
    assert client.get("/status", follow_redirects=False).status_code == 303
    client.cookies.set(auth.COOKIE, stolen)  # a copy of the cookie made before signing out
    assert client.get("/status", follow_redirects=False).status_code == 303


def test_home_goes_to_status(client):
    client.post("/login", data={"username": "admin", "password": PASSWORD})
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/status"


def test_non_ascii_username_does_not_crash(client):
    r = client.post("/login", data={"username": "admïn", "password": "x"})
    assert r.status_code == 401


def test_lockout_after_five_wrong_passwords(client):
    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "nope"})
    r = client.post("/login", data={"username": "admin", "password": PASSWORD})
    assert r.status_code == 429 and "Too many wrong attempts" in r.text


def test_login_never_redirects_off_site(client):
    r = client.post("/login", data={"username": "admin", "password": PASSWORD, "next": "https://evil.example"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"


def test_forged_cookie_rejected(client):
    client.cookies.set(auth.COOKIE, auth.make_session("admin", "sid", "not-the-secret", 3600))
    assert client.get("/status", follow_redirects=False).status_code == 303
    # Correctly signed but never started on the server: still rejected.
    client.cookies.set(auth.COOKIE, auth.make_session("admin", "made-up", "test-secret", 3600))
    assert client.get("/status", follow_redirects=False).status_code == 303


def test_huge_chart_signal_id_is_rejected_cleanly(client):
    client.post("/login", data={"username": "admin", "password": PASSWORD})
    assert client.get("/chart?signal=99999999999999999999").status_code == 422
