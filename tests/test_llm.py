"""LLM 高级功能测试：AI 精选/文件名兜底解析端点、结果缓存、未配置回退、Key 打码。"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DB_PATH", "/tmp/quark_stremio_test.db")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.config as config  # noqa: E402
import app.settings as settings  # noqa: E402
from app import db, llm  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
client.__enter__()

SECRET = Fernet.generate_key().decode()
AUTH = {}  # 由 tmp_db fixture 动态生成（注册管理员 + 会话）
GOOD_URL = "https://pan.quark.cn/s/f32d0e563a19"

_REVIEW_JSON = (
    '{"items":[{"index":0,"keep":true,"score":88,"dup_of":null,'
    '"reason":"完全匹配","title":"余罪","year":2016,"season":1},'
    '{"index":1,"keep":false,"score":null,"dup_of":0,"reason":"内容重复"}]}')


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    db.init(path, SECRET)
    monkeypatch.setattr(config, "DB_PATH", path)
    global AUTH
    from app.auth import hash_password, new_session_token, new_stream_token
    if not db.get_user_by_email(path, "admin@test.io"):
        db.create_user(path, "admin@test.io", hash_password("password123"),
                       "admin", "pro", new_stream_token())
    token = new_session_token()
    db.create_session(path, token,
                      db.get_user_by_email(path, "admin@test.io")["id"],
                      30 * 86400)
    AUTH = {"X-Session-Token": token}
    return path


@pytest.fixture()
def llm_on(tmp_db, monkeypatch):
    """启用 LLM 配置（teardown 还原全局 settings 缓存，避免污染其他测试）。"""
    monkeypatch.setattr(settings, "_cache", dict(settings._cache))
    settings.set(config.DB_PATH, {
        "LLM_API_BASE": "https://api.llm.test/v1",
        "LLM_API_KEY": "sk-test-1234567890",
        "LLM_MODEL": "test-model"})


def patch_chat(monkeypatch, reply=_REVIEW_JSON):
    """替换 llm.chat 记录调用次数（缓存命中验证用）。"""
    calls = {"n": 0}

    async def fake_chat(messages, *, temperature=0.2):
        calls["n"] += 1
        assert messages[0]["role"] == "system"  # skill 提示词在 system 位
        return reply

    monkeypatch.setattr(llm, "chat", fake_chat)
    return calls


# ---------- 未配置 LLM 时回退基础功能 ----------

def test_ai_endpoints_require_auth():
    assert client.post("/api/candidates/ai-review", json={}).status_code == 401
    assert client.post("/api/candidates/ai-parse", json={}).status_code == 401


def test_ai_endpoints_disabled_without_llm(tmp_db):
    """管理员未配置 LLM API → 501 明确提示（前端据 llm_enabled 隐藏入口）。"""
    assert not llm.configured()
    r = client.post("/api/candidates/ai-review", headers=AUTH,
                    json={"candidates": [{"title": "x"}]})
    assert r.status_code == 501 and "未配置" in r.json()["detail"]
    r = client.post("/api/candidates/ai-parse", headers=AUTH,
                    json={"names": [{"name": "06.mkv"}]})
    assert r.status_code == 501 and "未配置" in r.json()["detail"]


def test_configured_requires_all_three(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "_cache", dict(settings._cache))
    settings.set(config.DB_PATH, {"LLM_API_BASE": "https://x/v1",
                                  "LLM_API_KEY": "sk-x", "LLM_MODEL": "m"})
    assert llm.configured()
    settings.set(config.DB_PATH, {"LLM_MODEL": ""})
    assert not llm.configured()  # 任一项为空即视为未配置


# ---------- AI 精选：筛选/评分/去重 ----------

def test_ai_review_ok_and_cached(llm_on, monkeypatch):
    calls = patch_chat(monkeypatch)
    body = {"keyword": "余罪", "title": "余罪", "season": 1,
            "candidates": [
                {"title": "余罪 全集 4K", "source": "p1",
                 "published": "2026-01-01", "alive_state": "ok",
                 "video_count": 12, "total_size_h": "10GB"},
                {"title": "余罪 另一发布", "source": "p2"}]}
    r1 = client.post("/api/candidates/ai-review", json=body, headers=AUTH)
    assert r1.status_code == 200
    items = r1.json()["items"]
    assert items[0]["keep"] is True and items[0]["score"] == 88
    assert items[0]["title"] == "余罪" and items[0]["year"] == 2016
    assert items[1]["keep"] is False and items[1]["dup_of"] == 0
    assert calls["n"] == 1
    # 相同输入命中缓存，不再调用模型
    r2 = client.post("/api/candidates/ai-review", json=body, headers=AUTH)
    assert r2.status_code == 200 and r2.json() == r1.json()
    assert calls["n"] == 1
    # 不同输入 → 重新调用
    body2 = dict(body, keyword="回魂夜")
    client.post("/api/candidates/ai-review", json=body2, headers=AUTH)
    assert calls["n"] == 2


def test_ai_review_input_validation(llm_on, monkeypatch):
    patch_chat(monkeypatch)
    # candidates 缺失/为空/非列表 → 400
    for body in ({}, {"candidates": []}, {"candidates": "x"},
                 {"candidates": [{"title": "x"}], "season": "1"},
                 {"candidates": [{"title": "x"}], "season": True}):
        r = client.post("/api/candidates/ai-review", json=body, headers=AUTH)
        assert r.status_code == 400, body
    # 全部条目无效 → 400
    r = client.post("/api/candidates/ai-review", headers=AUTH,
                    json={"candidates": ["x", 1, None]})
    assert r.status_code == 400


def test_ai_review_model_error(llm_on, monkeypatch):
    """模型输出非法（非 JSON）→ 502 带原因。"""
    patch_chat(monkeypatch, reply="抱歉，我无法完成该任务")
    r = client.post("/api/candidates/ai-review", headers=AUTH,
                    json={"candidates": [{"title": "余罪"}]})
    assert r.status_code == 502 and "JSON" in r.json()["detail"]


def test_ai_review_sanitizes_model_output(llm_on, monkeypatch):
    """模型输出下标越界/类型不符的条目被丢弃，字段规整。"""
    patch_chat(monkeypatch, reply=(
        '{"items":[{"index":9,"keep":true},{"index":0,"keep":"yes",'
        '"score":"高","dup_of":0,"reason":"ok"},{"index":1,"keep":false,'
        '"dup_of":99}]}'))
    r = client.post("/api/candidates/ai-review", headers=AUTH,
                    json={"candidates": [{"title": "a"}, {"title": "b"}]})
    items = r.json()["items"]
    assert [it["index"] for it in items] == [0, 1]
    assert items[0]["keep"] is True and items[0]["score"] is None  # 非法分→None
    assert items[0]["dup_of"] is None  # dup_of 指向自身 → 无效
    assert items[1]["dup_of"] is None  # 下标越界 → None


# ---------- AI 兜底解析：无标题文件名 ----------

_PARSE_JSON = ('{"items":[{"index":0,"title":"狂飙","season":3,"episode":6,'
               '"confident":true},{"index":1,"title":null,"season":null,'
               '"episode":11,"confident":false}]}')


def test_ai_parse_ok_and_cached(llm_on, monkeypatch):
    calls = patch_chat(monkeypatch, reply=_PARSE_JSON)
    body = {"names": [
        {"name": "06.mkv", "search_title": "狂飙",
         "share_title": "狂飙 第三季 4K", "folder": "第三季"},
        {"name": "第11集.mp4"}]}
    r = client.post("/api/candidates/ai-parse", json=body, headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[0] == {"index": 0, "title": "狂飙", "season": 3,
                        "episode": 6, "confident": True}
    assert items[1]["title"] is None and items[1]["episode"] == 11
    assert calls["n"] == 1
    # 缓存命中
    client.post("/api/candidates/ai-parse", json=body, headers=AUTH)
    assert calls["n"] == 1
    # 文件名登记到解析问题名单（source=ai，供管理员跟进）
    issues = {it["name"]: it["source"]
              for it in db.list_name_issues(config.DB_PATH)}
    assert issues["06.mkv"] == "ai" and issues["第11集.mp4"] == "ai"


def test_ai_parse_input_validation(llm_on, monkeypatch):
    patch_chat(monkeypatch)
    for body in ({}, {"names": []}, {"names": "x"}, {"names": [{}]},
                 {"names": ["", "  "]}):
        r = client.post("/api/candidates/ai-parse", json=body, headers=AUTH)
        assert r.status_code == 400, body


# ---------- 搜索响应 llm_enabled 标志 ----------

def test_search_llm_enabled_flag(tmp_db, monkeypatch):
    """搜索响应带 llm_enabled（前端据此显示/隐藏 AI 精选入口）。"""
    import app.candidate_routes as cr

    async def fake_search(vtype, video_id, keyword="", disks=None, cookie=""):
        return None, [], "", keyword

    svc = cr.CandidateService()
    monkeypatch.setattr(svc, "search", fake_search)
    monkeypatch.setattr(cr, "candidate_service", svc)
    monkeypatch.setattr(settings, "_cache", dict(settings._cache))
    r = client.get("/api/candidates/search?kw=x", headers=AUTH)
    assert r.status_code == 200 and r.json()["llm_enabled"] is False
    settings.set(config.DB_PATH, {
        "LLM_API_BASE": "https://api.llm.test/v1",
        "LLM_API_KEY": "sk-test-1234567890",
        "LLM_MODEL": "test-model"})
    r = client.get("/api/candidates/search?kw=x", headers=AUTH)
    assert r.json()["llm_enabled"] is True


# ---------- 管理端：Key 回显打码 ----------

def test_config_llm_key_masked(llm_on):
    data = client.get("/api/admin/config", headers=AUTH).json()["config"]
    by_key = {it["key"]: it for it in data}
    assert by_key["LLM_API_KEY"]["value"] == "••••7890"  # 仅暴露末 4 位
    assert "sk-test-1234567890" not in str(data)
    assert by_key["LLM_API_BASE"]["value"] == "https://api.llm.test/v1"
    # 原样提交打码值 → 不覆盖真实 Key；提交新值 → 更新
    r = client.post("/api/admin/config", headers=AUTH,
                    json={"LLM_API_KEY": "••••7890", "LLM_MODEL": "new-model"})
    assert r.status_code == 200
    assert settings.get("LLM_API_KEY") == "sk-test-1234567890"
    assert settings.get("LLM_MODEL") == "new-model"
    r = client.post("/api/admin/config", headers=AUTH,
                    json={"LLM_API_KEY": "sk-new-key-000000"})
    assert settings.get("LLM_API_KEY") == "sk-new-key-000000"


# ---------- skill 输出解析 ----------

def test_extract_json_variants():
    obj = {"items": [1]}
    assert llm._extract_json('```json\n{"items": [1]}\n```') == obj
    assert llm._extract_json('好的，结果如下：{"items": [1]} 请查收') == obj
    assert llm._extract_json('{"items": [1]}') == obj
    with pytest.raises(llm.LLMError):
        llm._extract_json("完全不是 JSON")


# ---------- 思考链关闭 + 精选合并解析（需求第 1/3 条） ----------

def test_chat_payload_qwen_disables_thinking():
    """Qwen 思考型模型关闭思考链（DashScope 兼容模式参数 enable_thinking）；
    其他 provider 不附加该参数（不认此参数会 400）。"""
    p = llm._chat_payload("qwen3.7-flash", [{"role": "user", "content": "x"}], 0.2)
    assert p["enable_thinking"] is False
    assert "enable_thinking" not in llm._chat_payload(
        "gpt-4o", [{"role": "user", "content": "x"}], 0.2)


def test_validate_review_files_bounds():
    """逐文件解析输出校验：文件下标越界丢弃，字段规整。"""
    data = {"items": [{"index": 0, "keep": True, "files": [
        {"i": 0, "season": 1, "episode": 2, "canonical": "A.S01E02.mp4"},
        {"i": 9, "canonical": "B.mp4"},   # 越界 → 丢弃
        {"i": "x", "canonical": "C.mp4"},  # 非法 → 丢弃
    ]}]}
    out = llm._validate_review(data, 1, [1])
    assert len(out[0]["files"]) == 1
    assert out[0]["files"][0] == {"i": 0, "season": 1, "episode": 2,
                                  "canonical": "A.S01E02.mp4"}


@pytest.mark.usefixtures("tmp_db", "llm_on")
def test_llm_status_endpoint():
    """llm-status：前端据此把【搜索】按钮替换为【AI 精选】。"""
    assert client.get("/api/llm-status").status_code == 401
    r = client.get("/api/llm-status", headers=AUTH)
    assert r.status_code == 200 and r.json()["enabled"] is True


@pytest.mark.usefixtures("llm_on")
def test_ai_review_with_files_parse(monkeypatch):
    """精选+解析合并调用：输入带分享内文件路径，输出逐文件规范名/季集，
    服务端合并原名+规范名的规格后返回（前端直接渲染徽标）。"""
    calls = patch_chat(monkeypatch, reply=(
        '{"items":[{"index":0,"keep":true,"score":90,"dup_of":null,'
        '"reason":"匹配","title":"狂飙","year":2023,"season":3,'
        '"files":[{"i":0,"season":3,"episode":6,'
        '"canonical":"Kuang.Biao.S03E06.2023.2160p.WEB-DL.H.265.AAC.mp4"}]}]}'))
    r = client.post("/api/candidates/ai-review", headers=AUTH, json={
        "keyword": "狂飙", "title": "狂飙", "season": 3,
        "candidates": [{"title": "狂飙 第三季", "source": "p1",
                        "alive_state": "ok",
                        "files": [{"path": "第三季/06.mp4"},
                                  {"path": "海报.jpg"}]}]})
    assert r.status_code == 200
    it = r.json()["items"][0]
    assert it["files"][0]["canonical"] == \
        "Kuang.Biao.S03E06.2023.2160p.WEB-DL.H.265.AAC.mp4"
    assert it["files"][0]["source"] == "ai"
    assert it["files"][0]["season"] == 3 and it["files"][0]["episode"] == 6
    # 规格从规范名解析（分辨率/来源/编码），与原名合并
    spec = it["files"][0]["spec"]
    assert spec["resolution"] == "2160p" and spec["source"] == "WEB-DL" \
        and spec["codec"] == "H.265"
    assert calls["n"] == 1
    # 文件路径进入模型输入（缓存键随内容变化）
    client.post("/api/candidates/ai-review", headers=AUTH, json={
        "keyword": "狂飙", "title": "狂飙", "season": 3,
        "candidates": [{"title": "狂飙 第三季", "source": "p1",
                        "alive_state": "ok",
                        "files": [{"path": "第三季/07.mp4"}]}]})
    assert calls["n"] == 2
