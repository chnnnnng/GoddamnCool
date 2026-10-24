"""阶段 8：注册 / 登录 / 会话 / 密码修改测试（全部离线）。"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DB_PATH", "/tmp/quark_stremio_test.db")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.config as config  # noqa: E402
from app import db  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
SECRET = Fernet.generate_key().decode()
INVITE = "test-admin-token"


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    db.init(path, SECRET)
    monkeypatch.setattr(config, "DB_PATH", path)
    return path


def register(email="a@test.io", invite=INVITE, password="password123"):
    return client.post("/api/auth/register",
                       json={"email": email, "password": password,
                             "invite": invite})


def H(token):
    return {"X-Session-Token": token}


# ---------- 注册 ----------

def test_register_wrong_invite(tmp_db):
    r = register(invite="wrong")
    assert r.status_code == 403 and "邀请码" in r.json()["detail"]


def test_register_closed_without_admin_token(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    r = register()
    assert r.status_code == 403


def test_register_bad_email_or_password(tmp_db):
    assert register(email="no-at.com").status_code == 400
    assert register(password="short").status_code == 400


def test_first_user_admin_then_user_lite(tmp_db):
    r1 = register("first@test.io")
    assert r1.status_code == 200
    u1 = r1.json()["user"]
    assert u1["role"] == "admin" and u1["tier"] == "pro"
    r2 = register("second@test.io")
    u2 = r2.json()["user"]
    assert u2["role"] == "user" and u2["tier"] == "lite"


def test_register_duplicate_email(tmp_db):
    register("dup@test.io")
    r = register("dup@test.io")
    assert r.status_code == 409


# ---------- 登录 / 会话 ----------

def test_login_flow(tmp_db):
    register("login@test.io", password="password123")
    r = client.post("/api/auth/login",
                    json={"email": "login@test.io", "password": "wrong-pass"})
    assert r.status_code == 401
    r = client.post("/api/auth/login",
                    json={"email": "login@test.io", "password": "password123"})
    assert r.status_code == 200 and r.json()["token"]
    # 大小写不敏感
    r2 = client.post("/api/auth/login",
                     json={"email": "LOGIN@test.io", "password": "password123"})
    assert r2.status_code == 200


def test_me_requires_login(tmp_db):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me",
                      headers={"X-Session-Token": "invalid"}).status_code == 401


def test_me_payload_and_logout(tmp_db):
    token = register("me@test.io").json()["token"]
    r = client.get("/api/auth/me", headers=H(token))
    assert r.status_code == 200
    data = r.json()
    assert data["email"] == "me@test.io"
    assert data["cookie_bound"] is False
    assert data["quota"]["search"]["unlimited"] is True  # 首个用户是 admin
    assert data["quota"]["transfer"]["unlimited"] is True
    assert "/s/" in data["addon_url"]
    # 登出后会话失效
    assert client.post("/api/auth/logout", headers=H(token)).status_code == 200
    assert client.get("/api/auth/me", headers=H(token)).status_code == 401


def test_change_password(tmp_db):
    token = register("pw@test.io", password="oldpassword").json()["token"]
    # 原密码错误
    r = client.post("/api/me/password", headers=H(token),
                    json={"old_password": "wrong", "new_password": "newpassword1"})
    assert r.status_code == 400
    # 新密码太短
    r = client.post("/api/me/password", headers=H(token),
                    json={"old_password": "oldpassword", "new_password": "short"})
    assert r.status_code == 400
    # 修改成功后：旧密码登录失败，新密码成功
    r = client.post("/api/me/password", headers=H(token),
                    json={"old_password": "oldpassword", "new_password": "newpassword1"})
    assert r.status_code == 200
    assert client.post("/api/auth/login",
                       json={"email": "pw@test.io",
                             "password": "oldpassword"}).status_code == 401
    assert client.post("/api/auth/login",
                       json={"email": "pw@test.io",
                             "password": "newpassword1"}).status_code == 200
