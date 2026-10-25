"""阶段 8：管理员 API 测试（用户管理 + 运行时配置）。"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DB_PATH", "/tmp/quark_stremio_test.db")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.config as config  # noqa: E402
from app import db, settings  # noqa: E402
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


def register(email, password="password123"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": password, "invite": INVITE})
    assert r.status_code == 200
    return r.json()


def H(token):
    return {"X-Session-Token": token}


@pytest.fixture()
def admin(tmp_db):
    """首个注册用户自动是管理员。"""
    return register("admin@test.io")


# ---------- 权限 ----------

def test_admin_apis_require_admin(admin):
    # 未登录
    assert client.get("/api/admin/users").status_code == 401
    # 普通用户（第二个注册的是 lite）
    lite = register("lite@test.io")
    assert client.get("/api/admin/users", headers=H(lite["token"])).status_code == 403
    assert client.get("/api/admin/config", headers=H(lite["token"])).status_code == 403


# ---------- 用户管理 ----------

def test_create_modify_delete_user(admin):
    auth = H(admin["token"])
    # 建号
    r = client.post("/api/admin/users", headers=auth,
                    json={"email": "new@test.io", "password": "password123",
                          "tier": "pro", "role": "user"})
    assert r.status_code == 200
    uid = r.json()["id"]
    # 非法参数
    assert client.post("/api/admin/users", headers=auth,
                       json={"email": "bad", "password": "password123"}).status_code == 400
    assert client.post("/api/admin/users", headers=auth,
                       json={"email": "x@test.io", "password": "short"}).status_code == 400
    assert client.post("/api/admin/users", headers=auth,
                       json={"email": "x@test.io", "password": "password123",
                             "tier": "vip"}).status_code == 400
    # 列表
    users = client.get("/api/admin/users", headers=auth).json()["users"]
    u = next(x for x in users if x["id"] == uid)
    assert u["tier"] == "pro" and u["task_count"] == 0 and u["quota_used"] == 0
    # 改级别 + 重置密码
    assert client.post(f"/api/admin/users/{uid}", headers=auth,
                       json={"tier": "lite"}).status_code == 200
    assert client.post(f"/api/admin/users/{uid}", headers=auth,
                       json={"password": "newpassword1"}).status_code == 200
    assert client.post("/api/auth/login",
                       json={"email": "new@test.io",
                             "password": "newpassword1"}).status_code == 200
    users = client.get("/api/admin/users", headers=auth).json()["users"]
    assert next(x for x in users if x["id"] == uid)["tier"] == "lite"
    # 删除
    assert client.delete(f"/api/admin/users/{uid}", headers=auth).status_code == 200
    assert db.get_user(config.DB_PATH, uid) is None


def test_admin_self_protection(admin):
    auth = H(admin["token"])
    aid = admin["user"]["id"]
    assert client.post(f"/api/admin/users/{aid}", headers=auth,
                       json={"disabled": True}).status_code == 400
    assert client.post(f"/api/admin/users/{aid}", headers=auth,
                       json={"role": "user"}).status_code == 400
    assert client.delete(f"/api/admin/users/{aid}", headers=auth).status_code == 400


def test_disable_user_kicks_sessions(admin):
    auth = H(admin["token"])
    target = register("kick@test.io")
    assert client.get("/api/auth/me", headers=H(target["token"])).status_code == 200
    tid = target["user"]["id"]
    assert client.post(f"/api/admin/users/{tid}", headers=auth,
                       json={"disabled": True}).status_code == 200
    # 会话被清 → 401；登录也被拒
    assert client.get("/api/auth/me", headers=H(target["token"])).status_code == 401
    assert client.post("/api/auth/login",
                       json={"email": "kick@test.io",
                             "password": "password123"}).status_code == 403


# ---------- 运行时配置 ----------

def test_config_get_and_set(admin):
    auth = H(admin["token"])
    data = client.get("/api/admin/config", headers=auth).json()["config"]
    keys = {c["key"] for c in data}
    assert "LITE_DAILY_SEARCH_LIMIT" in keys and "APP_NAME" in keys
    meta = next(c for c in data if c["key"] == "LITE_DAILY_SEARCH_LIMIT")
    assert meta["meta"] and meta["value"] == 50
    keys_meta = {c["key"]: c["meta"] for c in data}
    assert keys_meta["TMDB_API_KEY"]  # TMDB key 由管理员在此填写
    assert keys_meta["LITE_DAILY_TRANSFER_LIMIT"]
    # 修改后 settings.get 立即生效
    r = client.post("/api/admin/config", headers=auth,
                    json={"LITE_DAILY_SEARCH_LIMIT": 7, "APP_NAME": "改名测试",
                          "LITE_DAILY_TRANSFER_LIMIT": 5,
                          "TMDB_API_KEY": "a" * 32})
    assert r.status_code == 200
    assert settings.get("LITE_DAILY_SEARCH_LIMIT") == 7
    assert settings.get("APP_NAME") == "改名测试"
    assert settings.get("LITE_DAILY_TRANSFER_LIMIT") == 5
    assert settings.get("TMDB_API_KEY") == "a" * 32
    # 持久化到 DB
    assert db.get_setting(config.DB_PATH, "LITE_DAILY_SEARCH_LIMIT") == 7
    # 非法键 / 非法值
    assert client.post("/api/admin/config", headers=auth,
                       json={"HACK_KEY": 1}).status_code == 400
    assert client.post("/api/admin/config", headers=auth,
                       json={"LITE_DAILY_SEARCH_LIMIT": -1}).status_code == 400
    assert client.post("/api/admin/config", headers=auth,
                       json={"LITE_DAILY_SEARCH_LIMIT": "abc"}).status_code == 400


# ---------- 名称解析问题名单（已移除 admin 面板入口，但 db helper 保留供内部调用） ----------
