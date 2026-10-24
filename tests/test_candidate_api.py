"""候选资源网页 API 与转存任务创建测试（阶段 3）。"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DB_PATH", "/tmp/quark_stremio_test.db")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.candidate_routes as cr  # noqa: E402
import app.config as config  # noqa: E402
from app import db  # noqa: E402
from app.candidate_service import CandidateService  # noqa: E402
from app.tmdb import VideoMeta  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.search import SearchCandidate, SearchError  # noqa: E402

client = TestClient(app)
client.__enter__()

SECRET = Fernet.generate_key().decode()
AUTH = {"X-Admin-Token": "test-admin-token"}
GOOD_URL = "https://pan.quark.cn/s/f32d0e563a19"


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    db.init(path, SECRET)
    monkeypatch.setattr(config, "DB_PATH", path)
    return path


_META_SENTINEL = object()


def patch_search(monkeypatch, candidates, errors="", meta=_META_SENTINEL):
    """替换 CandidateService.search，隔离外部来源与 TMDB。"""
    if meta is _META_SENTINEL:
        meta = VideoMeta({"imdb_id": "tt5636724", "name": "余罪",
                          "type": "series", "releaseInfo": "2016"})
    svc = CandidateService()
    calls = []

    async def fake_search(vtype, video_id, keyword=""):
        calls.append({"vtype": vtype, "id": video_id, "kw": keyword})
        return meta, candidates, errors, keyword or (meta.title if meta else "")

    monkeypatch.setattr(svc, "search", fake_search)
    monkeypatch.setattr(cr, "candidate_service", svc)
    svc.calls = calls
    return svc


def patch_fetch_meta(monkeypatch, meta=None):
    async def fake_fetch_meta(imdb_id, vtype, db_path=None):
        if imdb_id == "tt5636724":
            return meta or VideoMeta({"imdb_id": "tt5636724", "name": "余罪",
                                      "type": "series", "releaseInfo": "2016"})
        return None

    monkeypatch.setattr(cr, "fetch_meta", fake_fetch_meta)


# ---------- 搜索接口鉴权与结果 ----------

def test_search_requires_token():
    assert client.get("/api/candidates/search?vtype=movie&id=tt5636724").status_code == 401
    assert client.get("/api/candidates/search?vtype=movie&id=tt5636724",
                      headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_search_ok(monkeypatch):
    cands = [SearchCandidate(title="余罪 第1-2季", share_url=GOOD_URL, passcode="",
                             source="plugin:wanou", score=0.8)]
    svc = patch_search(monkeypatch, cands, errors="PanSou: 超时")
    r = client.get("/api/candidates/search?vtype=series&id=tt5636724:1:1", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["meta"]["title"] == "余罪"
    assert data["keyword"] == "余罪"  # 返回实际使用的关键词
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["share_url"] == GOOD_URL
    assert data["errors"] == "PanSou: 超时"  # 来源级错误透传展示
    # 手动关键词透传到服务层
    r2 = client.get("/api/candidates/search?vtype=series&id=tt5636724:1:1&kw=%E5%9B%9E%E9%AD%82%E5%A4%9C",
                    headers=AUTH)
    assert svc.calls[-1]["kw"] == "回魂夜"
    assert r2.json()["keyword"] == "回魂夜"


def test_search_meta_missing(monkeypatch):
    patch_search(monkeypatch, [], errors="TMDB 未收录 tt0000000",
                 meta=None)
    r = client.get("/api/candidates/search?vtype=movie&id=tt0000000", headers=AUTH)
    assert r.status_code == 404


# ---------- TMDB 元数据缓存（双语名） ----------

def test_tmdb_cache_roundtrip(tmp_db):
    data = {"imdb_id": "tt5925192", "name": "余罪", "name_zh": "余罪",
            "name_en": "Yu Zui", "type": "series", "releaseInfo": "2016"}
    assert db.get_tmdb_meta(tmp_db, "tt5925192", "series") is None
    db.save_tmdb_meta(tmp_db, "tt5925192", "series", data)
    got = db.get_tmdb_meta(tmp_db, "tt5925192", "series")
    assert got["name_en"] == "Yu Zui"
    # TTL 过期
    assert db.get_tmdb_meta(tmp_db, "tt5925192", "series", ttl=0) is None
    assert db.get_tmdb_meta(tmp_db, "tt0000000", "movie") is None


def test_score_prefers_matching_language_ref(monkeypatch):
    """跨语言：中文关键词下中文候选得分应高于仅凭英文原标题的得分。"""
    from app.candidate_service import score_candidate
    meta = VideoMeta({"imdb_id": "tt5925192", "name": "Yu Zui",
                      "type": "series", "releaseInfo": "2016"})
    c = SearchCandidate(title="【夸克网盘】余罪 第1-2季 (2016)", share_url=GOOD_URL)
    s_kw = score_candidate(c, meta, season=1, keyword="余罪")
    s_no_kw = score_candidate(c, meta, season=1, keyword="")
    assert s_kw > s_no_kw  # 关键词与候选同语言时得分更高
    assert s_kw >= 0.75   # 包含关键词本身


# ---------- 确认转存 / 任务创建 ----------

def _confirm(url=GOOD_URL, **kw):
    body = {"vtype": "series", "id": "tt5636724:1:12", "share_url": url,
            "passcode": "ax72", "title": "余罪 第1-2季", "source": "plugin:mizixing"}
    body.update(kw)
    return client.post("/api/candidates/confirm", json=body, headers=AUTH)


def test_confirm_requires_token():
    assert client.post("/api/candidates/confirm", json={}).status_code == 401


def test_confirm_rejects_bad_share_url(monkeypatch):
    patch_fetch_meta(monkeypatch)
    for bad in ("https://evil.example/s/xxxx", "http://pan.quark.cn/s/abc",
                "https://pan.baidu.com/s/1abcdef", GOOD_URL + "XX"):
        r = _confirm(url=bad)
        assert r.status_code == 400, bad
        assert "分享链接无效" in r.json()["detail"]


def test_confirm_creates_task_idempotent(tmp_db, monkeypatch):
    patch_fetch_meta(monkeypatch)
    r = _confirm()
    assert r.status_code == 200
    data = r.json()
    assert data["created"] is True and data["status"] == "PENDING"
    task = db.get_task(tmp_db, data["task_id"])
    assert task["imdb_id"] == "tt5636724" and task["season"] == 1 and task["episode"] == 12
    assert task["title"] == "余罪" and task["passcode"] == "ax72"
    # 幂等：同一分享 + 同集数的活跃任务不重复创建
    r2 = _confirm()
    assert r2.json()["task_id"] == data["task_id"] and r2.json()["created"] is False
    # 不同集数 → 新任务
    r3 = _confirm(**{"id": "tt5636724:1:13"})
    assert r3.json()["created"] is True and r3.json()["task_id"] != data["task_id"]


def test_confirm_meta_failure(tmp_db, monkeypatch):
    patch_fetch_meta(monkeypatch, meta=None)
    r = _confirm(**{"id": "tt9999999:1:1"})
    assert r.status_code == 400 and "元数据" in r.json()["detail"]


def test_task_api_auth_and_404(tmp_db, monkeypatch):
    patch_fetch_meta(monkeypatch)
    task_id = _confirm().json()["task_id"]
    assert client.get(f"/api/task/{task_id}").status_code == 401
    assert client.get("/api/task/nonexistent-id", headers=AUTH).status_code == 404
    r = client.get(f"/api/task/{task_id}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "PENDING" and r.json()["status_text"] == "等待执行"


# ---------- 候选存活检测（PanSou /api/check/links 代理） ----------

def test_verify_requires_token():
    assert client.post("/api/candidates/verify", json={"items": []}).status_code == 401


def test_verify_rejects_empty_and_invalid(monkeypatch):
    # items 缺失/为空
    for body in ({}, {"items": []}):
        r = client.post("/api/candidates/verify", json=body, headers=AUTH)
        assert r.status_code == 400
    # 全部为非法链接（不送检）
    r = client.post("/api/candidates/verify", headers=AUTH,
                    json={"items": [{"share_url": "https://evil.example/s/xxxx"}]})
    assert r.status_code == 400


def test_verify_maps_states(monkeypatch):
    svc = CandidateService()
    calls = []

    async def fake_verify(items):
        calls.append(items)
        return [{"share_url": i["share_url"],
                 "state": "ok" if i["share_url"] == GOOD_URL else "bad",
                 "summary": "链接有效"} for i in items]

    monkeypatch.setattr(svc, "verify_links", fake_verify)
    monkeypatch.setattr(cr, "candidate_service", svc)
    r = client.post("/api/candidates/verify", headers=AUTH, json={"items": [
        {"share_url": GOOD_URL, "passcode": "ax72"},
        {"share_url": "https://pan.quark.cn/s/2a5efb0c3dc7", "passcode": ""},
        # 非法链接应被过滤，不进入 verify_links
        {"share_url": "https://pan.baidu.com/s/1abcdef"},
    ]})
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 2
    assert [x["state"] for x in results] == ["ok", "bad"]
    assert calls[0][0]["passcode"] == "ax72"


async def test_verify_links_degrades_to_uncertain(monkeypatch):
    """PanSou 检测整体失败时降级为 uncertain，不向上抛异常。"""
    from app import candidate_service as cs_mod

    async def fake_check(self, items):
        raise SearchError("检测服务不可用")

    monkeypatch.setattr(cs_mod.PanSouProvider, "check_links", fake_check)
    svc = cs_mod.CandidateService()
    res = await svc.verify_links([
        {"share_url": GOOD_URL, "passcode": ""},
        {"share_url": "https://pan.quark.cn/s/2a5efb0c3dc7", "passcode": "ax72"},
    ])
    assert [x["state"] for x in res] == ["uncertain", "uncertain"]
    assert "不可用" in res[0]["summary"]


async def test_verify_links_order_and_passcode(monkeypatch):
    """结果按输入顺序返回；有码条目带 password 字段。"""
    from app import candidate_service as cs_mod
    seen = []

    async def fake_check(self, items):
        seen.extend(items)
        return [{"disk_type": "quark", "url": i["url"], "state": "ok",
                 "summary": "链接有效"} for i in items]

    monkeypatch.setattr(cs_mod.PanSouProvider, "check_links", fake_check)
    svc = cs_mod.CandidateService()
    res = await svc.verify_links([
        {"share_url": "https://pan.quark.cn/s/2a5efb0c3dc7", "passcode": "ax72"},
        {"share_url": GOOD_URL, "passcode": ""},
    ])
    assert [x["share_url"] for x in res] == \
        ["https://pan.quark.cn/s/2a5efb0c3dc7", GOOD_URL]
    assert seen[0]["password"] == "ax72"
    assert "password" not in seen[1]  # 无提取码不传该字段


# ---------- 搜索流程：双名搜索 / 存活检测沉底 ----------

URL_A = "https://pan.quark.cn/s/f32d0e563a19"
URL_B = "https://pan.quark.cn/s/2a5efb0c3dc7"


def patch_real_search(monkeypatch, tmp_db, name_zh, name_en=None):
    """mock fetch_meta（TMDB 双语名）与 PanSouProvider（search 记录关键词、check 可定制）。"""
    from app import candidate_service as cs_mod
    meta = VideoMeta({"imdb_id": "tt5925192", "name": name_zh,
                      "name_zh": name_zh, "name_en": name_en or name_zh,
                      "type": "series", "releaseInfo": "2016"})

    async def fake_fetch_meta(imdb_id, vtype, db_path=None):
        return meta

    monkeypatch.setattr(cs_mod, "fetch_meta", fake_fetch_meta)
    calls = []

    async def fake_search(self, kw, limit=20):
        calls.append(kw)
        if kw == name_zh:  # 中文名批次：A 命中 + B
            return [SearchCandidate(title=f"{name_zh} 全集 4K", share_url=URL_A,
                                    passcode="ax72", source="p1", published="2026-09-24"),
                    SearchCandidate(title=f"{name_zh} 已失效资源", share_url=URL_B)]
        return [SearchCandidate(title="Yu Zui S01-02 1080p", share_url=URL_B,
                                source="p2", published="2026-09-25"),  # 与中文批次重复
                SearchCandidate(title="Yu Zui 全年合集", share_url="https://pan.quark.cn/s/aaaaaaaaaa")]

    async def fake_check(self, items):
        return [{"disk_type": "quark", "url": i["url"],
                 "state": "bad" if i["url"] == URL_B else "ok",
                 "summary": "分享不存在" if i["url"] == URL_B else "链接有效"}
                for i in items]

    monkeypatch.setattr(cs_mod.PanSouProvider, "search", fake_search)
    monkeypatch.setattr(cs_mod.PanSouProvider, "check_links", fake_check)
    return calls


async def test_search_dual_keywords_merge_and_dead_last(tmp_db, monkeypatch):
    """TMDB 双语名（中文名 ≠ 外语名）→ 两个名字各搜一次；
    跨关键词去重；失效候选沉底且带存活状态。"""
    calls = patch_real_search(monkeypatch, tmp_db, "余罪", "Yu Zui")
    svc = CandidateService(db_path=tmp_db)
    m, cands, errs, kw = await svc.search("series", "tt5925192")
    assert calls == ["余罪", "Yu Zui"]  # 中文名 + 外语名各一次
    assert kw == "余罪"
    urls = [c.share_url for c in cands]
    assert len(urls) == len(set(urls)) and len(urls) == 3  # 跨关键词去重
    assert cands[0].share_url == URL_A  # ok 组在前（组内按匹配度）
    assert cands[0].alive_state == "ok" and cands[0].alive_summary == "链接有效"
    assert cands[-1].share_url == URL_B and cands[-1].alive_state == "bad"
    assert cands[-1].alive_summary == "分享不存在"


async def test_search_single_keyword_when_names_match(tmp_db, monkeypatch):
    """TMDB 双语名相同（无译名影片）→ 只搜一次，不做无谓的第二查。"""
    calls = patch_real_search(monkeypatch, tmp_db, "FRIENDS")
    svc = CandidateService(db_path=tmp_db)
    await svc.search("series", "tt5925192")
    assert calls == ["FRIENDS"]


async def test_search_manual_keyword_merges_with_tmdb_names(tmp_db, monkeypatch):
    """手动关键词与 TMDB 中文名归一化相同 → 去重合并，只补搜外语名；
    手动关键词不做缓存。"""
    calls = patch_real_search(monkeypatch, tmp_db, "余罪", "Yu Zui")
    svc = CandidateService(db_path=tmp_db)
    m, cands, errs, kw = await svc.search("series", "tt5925192", keyword="余罪")
    assert calls == ["余罪", "Yu Zui"]
    assert kw == "余罪"


async def test_search_alive_check_failure_degrades(tmp_db, monkeypatch):
    """存活检测整体失败：候选照常返回，全部标记 uncertain（不抛异常）。"""
    from app import candidate_service as cs_mod
    patch_real_search(monkeypatch, tmp_db, "余罪", "Yu Zui")

    async def fake_check(self, items):
        raise SearchError("检测服务不可用")

    monkeypatch.setattr(cs_mod.PanSouProvider, "check_links", fake_check)
    svc = CandidateService(db_path=tmp_db)
    m, cands, errs, kw = await svc.search("series", "tt5925192")
    assert len(cands) == 3
    assert all(c.alive_state == "uncertain" for c in cands)


# ---------- 任务状态机（六态） ----------

def test_task_status_six_states(tmp_db, monkeypatch):
    patch_fetch_meta(monkeypatch)
    task_id = _confirm().json()["task_id"]
    for status in db.TASK_STATUSES:
        assert db.update_task_status(tmp_db, task_id, status, error="e" if status == "FAILED" else "")
        assert db.get_task(tmp_db, task_id)["status"] == status
    with pytest.raises(ValueError):
        db.update_task_status(tmp_db, task_id, "NOT_A_STATUS")


def test_create_task_reactivates_after_terminal(tmp_db, monkeypatch):
    """活跃任务完成后，同链接可再次创建新任务。"""
    patch_fetch_meta(monkeypatch)
    t1 = _confirm().json()["task_id"]
    db.update_task_status(tmp_db, t1, "EXPIRED")
    t2, created = db.create_task(
        tmp_db, vtype="series", imdb_id="tt5636724", season=1, episode=12,
        title="余罪", candidate_title="x", share_url=GOOD_URL)
    assert created and t2 != t1


# ---------- 页面骨架 ----------

def test_pages_render():
    for path in ("/candidates", "/task/some-id"):
        r = client.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_pages_script_tags_balanced():
    """回归：</script> 闭合标签丢失会让整个脚本不执行，页面永远停在加载中。"""
    for path in ("/candidates", "/task/some-id"):
        html = client.get(path).text
        assert html.count("<script>") == html.count("</script>") == 1
        assert html.rstrip().endswith("</html>")
