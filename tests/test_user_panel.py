"""阶段 8：用户面板 API 测试（Cookie 绑定/检测、保存目录、令牌轮换、我的任务）。"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DB_PATH", "/tmp/quark_stremio_test.db")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.config as config  # noqa: E402
import app.user_routes as ur  # noqa: E402
from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.base import ProviderError  # noqa: E402

client = TestClient(app)
SECRET = Fernet.generate_key().decode()
INVITE = "test-admin-token"


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    db.init(path, SECRET)
    monkeypatch.setattr(config, "DB_PATH", path)
    return path


class FakeProvider:
    """verify 按记录：cookie 含 "bad" 视为登录态失效。"""

    def __init__(self, cookie=None, db_path=None, user_id=None):
        self.cookie = cookie
        self.user_id = user_id

    async def verify(self):
        if "bad" in self.cookie:
            raise ProviderError("登录态失效")

    async def close(self):
        pass


def register(email="u@test.io"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123",
                          "invite": INVITE})
    assert r.status_code == 200
    return r.json()


def H(token):
    return {"X-Session-Token": token}


@pytest.fixture()
def user(tmp_db, monkeypatch):
    monkeypatch.setattr(ur, "QuarkDriveProvider", FakeProvider)
    return register()


# ---------- 网盘 Cookie 绑定 ----------

def test_bind_and_unbind_cookie(user, tmp_db):
    uid = user["user"]["id"]
    r = client.post("/api/me/cookie", headers=H(user["token"]),
                    json={"cookie": '  "kps=1; sign=2; __puus=x"  '})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert db.load_user_cookie(tmp_db, uid) == "kps=1; sign=2; __puus=x"
    # 状态回显
    assert client.get("/api/auth/me", headers=H(user["token"])
                      ).json()["cookie_bound"] is True
    # 解绑
    assert client.delete("/api/me/cookie", headers=H(user["token"])).status_code == 200
    assert db.load_user_cookie(tmp_db, uid) is None


def test_bind_cookie_rejected_without_puus(user):
    r = client.post("/api/me/cookie", headers=H(user["token"]),
                    json={"cookie": "kps=1; sign=2"})
    assert r.status_code == 400 and "__puus" in r.json()["detail"]


def test_bind_cookie_verify_failed(user, tmp_db):
    r = client.post("/api/me/cookie", headers=H(user["token"]),
                    json={"cookie": "bad-cookie; __puus=x"})
    assert r.status_code == 400 and "Cookie 验证失败" in r.json()["detail"]
    assert db.load_user_cookie(tmp_db, user["user"]["id"]) is None


def test_check_cookie_state(user):
    Hh = H(user["token"])
    # 未绑定时检测 → 400
    assert client.post("/api/me/cookie/check", headers=Hh).status_code == 400
    # 绑定后检测 → 有效
    assert client.post("/api/me/cookie", headers=Hh,
                       json={"cookie": "kps=1; __puus=x"}).status_code == 200
    r = client.post("/api/me/cookie/check", headers=Hh)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "有效" in r.json()["message"]


# ---------- 保存目录 ----------

def test_set_and_clear_save_dir(user):
    uid = user["user"]["id"]
    r = client.post("/api/me/save_dir", headers=H(user["token"]),
                    json={"save_dir": "  我的影视  "})
    assert r.status_code == 200 and r.json()["save_dir"] == "我的影视"
    assert db.get_user(tmp_path_db(user), uid)["save_dir"] == "我的影视"
    assert client.get("/api/auth/me", headers=H(user["token"])
                      ).json()["save_dir"] == "我的影视"
    # 留空 = 恢复默认
    r = client.post("/api/me/save_dir", headers=H(user["token"]),
                    json={"save_dir": ""})
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=H(user["token"])).json()["save_dir"] == ""


def tmp_path_db(user):
    import app.config as config
    return config.DB_PATH


# ---------- Add-on 令牌轮换 ----------

def test_rotate_token(user):
    old_url = client.get("/api/auth/me", headers=H(user["token"])).json()["addon_url"]
    old_token = old_url.split("/s/")[1].split("/")[0]
    r = client.post("/api/me/token/rotate", headers=H(user["token"]))
    assert r.status_code == 200 and r.json()["addon_url"] != old_url
    new_token = r.json()["addon_url"].split("/s/")[1].split("/")[0]
    assert client.get(f"/s/{old_token}/manifest.json").status_code == 404
    assert client.get(f"/s/{new_token}/manifest.json").status_code == 200


def test_unknown_addon_token_404(tmp_db):
    assert client.get("/s/deadbeef/manifest.json").status_code == 404


# ---------- 我的任务 ----------

def test_my_tasks_isolated(user, tmp_db):
    uid = user["user"]["id"]
    db.create_task(tmp_db, vtype="series", imdb_id="tt1", season=1, episode=1,
                   title="我的任务", candidate_title="c", share_url="https://pan.quark.cn/s/aaa",
                   user_id=uid)
    db.create_task(tmp_db, vtype="series", imdb_id="tt2", season=None, episode=None,
                   title="别人的任务", candidate_title="c",
                   share_url="https://pan.quark.cn/s/bbb", user_id="other-user")
    r = client.get("/api/me/tasks", headers=H(user["token"]))
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert len(tasks) == 1 and tasks[0]["title"] == "我的任务"
    assert tasks[0]["status_text"] == "等待执行"
