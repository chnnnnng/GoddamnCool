"""阶段 8：Lite 每日搜索配额测试。"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DB_PATH", "/tmp/quark_stremio_test.db")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.candidate_routes as cr  # noqa: E402
import app.config as config  # noqa: E402
from app import db, settings  # noqa: E402
from app.candidate_service import CandidateService  # noqa: E402
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


@pytest.fixture()
def patched_search(monkeypatch):
    """隔离外部来源与 TMDB：search 恒返回一个候选。"""
    svc = CandidateService()

    async def fake_search(vtype, video_id, keyword="", disks=None, cookie=""):
        return None, [], "", keyword

    monkeypatch.setattr(svc, "search", fake_search)
    monkeypatch.setattr(cr, "candidate_service", svc)
    return svc


def register(email):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123",
                          "invite": INVITE})
    assert r.status_code == 200
    return r.json()


def H(token):
    return {"X-Session-Token": token}


def search(token, kw="kw"):
    return client.get(f"/api/candidates/search?kw={kw}", headers=H(token))


def test_lite_quota_enforced(tmp_db, patched_search):
    admin = register("admin@test.io")       # 首个用户：admin + pro，不限
    lite = register("lite@test.io")         # 第二个用户：lite
    settings.set(tmp_db, {"LITE_DAILY_SEARCH_LIMIT": 3})
    for _ in range(3):
        assert search(lite["token"]).status_code == 200
    r = search(lite["token"])
    assert r.status_code == 429
    assert "升级 Pro" in r.json()["detail"]
    # 计次只发生在参数校验通过后：3 次（不含被拒的那次）
    assert db.get_search_count(tmp_db, lite["user"]["id"]) == 3
    # admin / pro 不受限
    for _ in range(5):
        assert search(admin["token"]).status_code == 200
    # 管理员把 lite 升 pro 后立即解除限制
    client.post(f"/api/admin/users/{lite['user']['id']}", headers=H(admin["token"]),
                json={"tier": "pro"})
    assert search(lite["token"]).status_code == 200


def test_quota_not_counted_on_param_error(tmp_db, patched_search):
    register("admin@test.io")
    lite = register("lite@test.io")
    settings.set(tmp_db, {"LITE_DAILY_SEARCH_LIMIT": 3})
    # 参数错误（无关键词）不计次
    assert client.get("/api/candidates/search",
                      headers=H(lite["token"])).status_code == 400
    assert db.get_search_count(tmp_db, lite["user"]["id"]) == 0


# ---------- Lite 每日转存配额 ----------

def confirm(token, url, **kw):
    body = {"share_url": url, "title": "测试资源", **kw}
    return client.post("/api/candidates/confirm", headers=H(token), json=body)


def test_lite_transfer_quota(tmp_db):
    admin = register("admin@test.io")
    lite = register("lite@test.io")
    settings.set(tmp_db, {"LITE_DAILY_TRANSFER_LIMIT": 3})
    urls = [f"https://pan.quark.cn/s/{i:012x}" for i in range(5)]
    for i in range(3):
        assert confirm(lite["token"], urls[i]).status_code == 200
    r = confirm(lite["token"], urls[3])
    assert r.status_code == 429 and "转存次数" in r.json()["detail"]
    # 幂等复用（同分享重复确认）不受限也不额外计次
    assert confirm(lite["token"], urls[0]).status_code == 200
    assert db.get_transfer_count(tmp_db, lite["user"]["id"]) == 3
    # pro 不受限
    client.post(f"/api/admin/users/{lite['user']['id']}",
                headers=H(admin["token"]), json={"tier": "pro"})
    assert confirm(lite["token"], urls[4]).status_code == 200
