import os, json, tempfile
_tmp = tempfile.mkdtemp()
os.environ["NOTION_ACCOUNTS"] = json.dumps([{"token_v2": "tok_ABCDEFGH1234", "space_id": "s1", "user_id": "u1", "user_email": "a@x"}])
os.environ["API_KEY"] = "test-key"
from cryptography.fernet import Fernet
os.environ["TOKEN_ENC_KEY"] = Fernet.generate_key().decode()
os.environ["ADMIN_DB_PATH"] = os.path.join(_tmp, "admin.db")
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "correct horse"
os.environ["ADMIN_LOCKOUT_MAX_FAILURES"] = "3"

import pytest
from fastapi.testclient import TestClient
from app.server import app

COOKIE = "__Host-n2a_session"


@pytest.fixture(scope="module")
def c():
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def login(c, ip="1.1.1.1", pw="correct horse"):
    return c.post("/admin/api/login", json={"username": "admin", "password": pw},
                  headers={"X-Forwarded-For": ip})


def test_public_surface(c):
    assert c.get("/health").json() == {"status": "ok"}
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(p).status_code == 404
    assert c.get("/v1/models").status_code == 401
    assert c.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/v1/models", headers={"Authorization": "Bearer test-key"}).status_code == 200
    r = c.get("/chat/", follow_redirects=False)
    assert r.status_code == 302
    assert c.get("/admin/api/accounts").status_code == 401


def test_login_csrf_and_accounts(c):
    r = login(c)
    assert r.status_code == 200
    sc = r.headers["set-cookie"]
    assert "HttpOnly" in sc and "Secure" in sc and "samesite=strict" in sc.lower()
    csrf = r.json()["csrf"]
    accs = c.get("/admin/api/accounts").json()["items"]
    assert len(accs) == 1 and accs[0]["token_hint"] == "…1234"
    assert "tok_ABCDEFGH1234" not in json.dumps(accs)
    # CSRF required
    assert c.post("/admin/api/accounts", json={"token_v2": "x" * 20, "space_id": "s2", "user_id": "u2"}).status_code == 403
    r = c.post("/admin/api/accounts", json={"token_v2": "y" * 20 + "WXYZ", "space_id": "s2", "user_id": "u2"},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert len(app.state.account_pool.clients) == 2
    new_id = r.json()["id"]
    assert c.patch(f"/admin/api/accounts/{new_id}", json={"enabled": False}, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert len(app.state.account_pool.clients) == 1
    # cross-origin rejected
    assert c.delete(f"/admin/api/accounts/{new_id}", headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"}).status_code == 403
    assert c.delete(f"/admin/api/accounts/{new_id}", headers={"X-CSRF-Token": csrf}).status_code == 200
    # session works for /v1 GET; chat page accessible
    assert c.get("/v1/models").status_code == 200
    assert c.get("/chat/").status_code == 200
    # DB stores token encrypted
    import sqlite3
    raw = open(os.environ["ADMIN_DB_PATH"], "rb").read()
    assert b"tok_ABCDEFGH1234" not in raw
    for k in ("/admin/api/overview", "/admin/api/stats?days=7", "/admin/api/requests"):
        assert c.get(k).status_code == 200
    c.post("/admin/api/logout", headers={"X-CSRF-Token": csrf})
    c.cookies.clear()
    assert c.get("/admin/api/accounts").status_code == 401


def test_lockout(c, monkeypatch):
    from app.admin import security as sec
    monkeypatch.setattr(sec, "client_ip", lambda req: req.headers.get("x-forwarded-for", "?"))
    c.cookies.clear()
    for _ in range(2):
        assert login(c, ip="9.9.9.9", pw="nope").status_code == 401
    assert login(c, ip="9.9.9.9", pw="nope").status_code == 429
    assert login(c, ip="9.9.9.9").status_code == 429  # even correct password while locked
    assert login(c, ip="8.8.8.8").status_code == 200  # other IP unaffected
    c.cookies.clear()


def test_client_ip_trusts_xff_only_from_private_peer():
    from app.admin.security import client_ip
    class R:
        def __init__(s, peer, xff): s.client = type("C", (), {"host": peer}); s.headers = {"x-forwarded-for": xff}
    assert client_ip(R("172.31.81.1", "5.5.5.5, 6.6.6.6")) == "6.6.6.6"
    assert client_ip(R("8.8.4.4", "6.6.6.6")) == "8.8.4.4"


def test_global_lockout(monkeypatch):
    from app.admin import security as sec
    monkeypatch.setattr(sec, "GLOBAL_MAX_FAILURES", 4)
    lk = sec.LoginLockout()
    for i in range(4):
        lk.record_failure(f"10.0.0.{i}")
    assert lk.locked_for("10.9.9.9") > 0
