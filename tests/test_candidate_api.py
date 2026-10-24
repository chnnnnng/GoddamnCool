"""候选资源网页 API 与转存任务创建测试（阶段 3）。"""
import json
import os
import re

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

    async def fake_search(vtype, video_id, keyword="", disks=None):
        calls.append({"vtype": vtype, "id": video_id, "kw": keyword,
                      "disks": disks})
        return (meta if video_id else None), candidates, errors, \
            keyword or (meta.title if meta and video_id else "")

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


# ---------- 独立搜索（无影片 ID）与网盘类型筛选 ----------

def test_independent_search_requires_keyword(monkeypatch):
    """独立搜索模式：无 id 且无关键词 → 400。"""
    r = client.get("/api/candidates/search", headers=AUTH)
    assert r.status_code == 400 and "关键词" in r.json()["detail"]


def test_independent_search_ok(monkeypatch):
    """无 id：仅按关键词搜索一次，meta 为 null，候选带 disk 字段。"""
    cands = [SearchCandidate(title="某剧 全集 4K", share_url=GOOD_URL,
                             source="plugin:x", score=0.8)]
    svc = patch_search(monkeypatch, cands)
    r = client.get("/api/candidates/search?kw=%E4%BD%99%E7%BD%AA", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["meta"] is None
    assert data["keyword"] == "余罪"
    assert data["candidates"][0]["disk"] == "quark"
    assert svc.calls[-1] == {"vtype": "movie", "id": "", "kw": "余罪",
                             "disks": None}  # 缺省 disks → Provider 默认仅夸克


def test_search_disks_param(monkeypatch):
    """disks 参数：all → 不限（[]）；缺省 → None（Provider 默认夸克）；列表透传；未知 400。"""
    svc = patch_search(monkeypatch, [])
    r = client.get("/api/candidates/search?kw=x&disks=all", headers=AUTH)
    assert r.status_code == 200 and svc.calls[-1]["disks"] == []
    r = client.get("/api/candidates/search?kw=x", headers=AUTH)
    assert r.status_code == 200 and svc.calls[-1]["disks"] is None
    r = client.get("/api/candidates/search?kw=x&disks=quark,uc", headers=AUTH)
    assert r.status_code == 200 and svc.calls[-1]["disks"] == ["quark", "uc"]
    r = client.get("/api/candidates/search?kw=x&disks=foobar", headers=AUTH)
    assert r.status_code == 400 and "网盘类型" in r.json()["detail"]


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

    async def fake_search(self, kw, limit=20, cloud_types=None):
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


# ---------- 整包转存 mode（阶段 5：转存完整剧集 / 仅转存所选集） ----------

def test_confirm_whole_creates_unseasoned_task(tmp_db, monkeypatch):
    patch_fetch_meta(monkeypatch)
    r = _confirm(mode="whole")
    assert r.status_code == 200 and r.json()["created"] is True
    task = db.get_task(tmp_db, r.json()["task_id"])
    assert task["mode"] == "whole"
    assert task["season"] is None and task["episode"] is None  # 不锚定季集


def test_confirm_whole_idempotent_and_coexists_with_episode(tmp_db, monkeypatch):
    """whole 幂等键 = 分享 + 影片（不带季集）；与特定集任务可并存。"""
    patch_fetch_meta(monkeypatch)
    r1 = _confirm(mode="whole")
    r2 = _confirm(mode="whole")
    assert r2.json()["task_id"] == r1.json()["task_id"]
    assert r2.json()["created"] is False
    r3 = _confirm()  # episode 模式 S1E12 → 不同幂等键，新任务
    assert r3.json()["created"] is True and r3.json()["task_id"] != r1.json()["task_id"]


def test_confirm_rejects_invalid_mode(tmp_db, monkeypatch):
    patch_fetch_meta(monkeypatch)
    r = _confirm(mode="bulk")
    assert r.status_code == 400 and "mode" in r.json()["detail"]


# ---------- 独立转存（share-only 任务，无影片 ID） ----------

def test_confirm_independent_share_only(tmp_db, monkeypatch):
    """无 id/vtype：share-only 任务（imdb_id 为空），季集可由 body 提供。"""
    r = client.post("/api/candidates/confirm", headers=AUTH, json={
        "share_url": GOOD_URL, "passcode": "ax72",
        "title": "某剧 合集", "source": "plugin:x", "mode": "episode",
        "season": 2, "episode": 6})
    assert r.status_code == 200 and r.json()["created"] is True
    task = db.get_task(tmp_db, r.json()["task_id"])
    assert task["video_type"] == "" and task["imdb_id"] == ""
    assert task["season"] == 2 and task["episode"] == 6
    assert task["title"] == "某剧 合集"


def test_confirm_independent_whole_and_no_anchor_mix(tmp_db):
    """独立 whole 模式合法；带 id 但缺 vtype（或反之）→ 400。"""
    r = client.post("/api/candidates/confirm", headers=AUTH, json={
        "share_url": GOOD_URL, "title": "合集", "mode": "whole"})
    assert r.status_code == 200 and db.get_task(
        tmp_db, r.json()["task_id"])["mode"] == "whole"
    for body in ({"vtype": "movie", "share_url": GOOD_URL},
                 {"id": "tt5636724:1:1", "share_url": GOOD_URL},
                 {"vtype": "tv", "id": "tt1", "share_url": GOOD_URL},
                 {"share_url": GOOD_URL, "mode": "episode", "season": "x"}):
        r = client.post("/api/candidates/confirm", headers=AUTH, json=body)
        assert r.status_code == 400, body


def test_whole_task_covers_all_episodes(tmp_path):
    """get_tasks_for_video：整包任务对该影片任意季集生效，与特定集任务并存。"""
    path = str(tmp_path / "t.db")
    db.init(path, SECRET)
    db.create_task(path, vtype="series", imdb_id="tt1", season=None, episode=None,
                   title="T", candidate_title="c", share_url="u1", mode="whole")
    db.create_task(path, vtype="series", imdb_id="tt1", season=1, episode=1,
                   title="T", candidate_title="c", share_url="u2")
    assert len(db.get_tasks_for_video(path, "series", "tt1", 1, 2)) == 1   # 仅整包
    assert len(db.get_tasks_for_video(path, "series", "tt1", 1, 1)) == 2   # 整包+特定集
    assert len(db.get_tasks_for_video(path, "series", "tt1", 3, 1)) == 1   # 其他季也覆盖


# ---------- 分享内容解析（阶段 5：查看内容详情） ----------

def patch_inspect(monkeypatch, result=None, error=None):
    svc = CandidateService()
    calls = []

    async def fake_inspect(share_url, passcode=""):
        calls.append({"url": share_url, "passcode": passcode})
        if error:
            raise RuntimeError(error)
        return result or {"video_count": 1, "other_count": 0, "total_size": 1000,
                          "total_size_h": "1000B", "is_series": False,
                          "seasons": [], "files": []}

    monkeypatch.setattr(svc, "inspect_share", fake_inspect)
    monkeypatch.setattr(cr, "candidate_service", svc)
    svc.inspect_calls = calls
    return svc


def test_inspect_requires_token():
    assert client.post("/api/candidates/inspect",
                       json={"share_url": GOOD_URL}).status_code == 401


def test_inspect_rejects_invalid_url():
    r = client.post("/api/candidates/inspect", headers=AUTH,
                    json={"share_url": "https://pan.baidu.com/s/1abcdef"})
    assert r.status_code == 400 and "分享链接无效" in r.json()["detail"]


def test_inspect_ok(monkeypatch):
    svc = patch_inspect(monkeypatch)
    r = client.post("/api/candidates/inspect", headers=AUTH,
                    json={"share_url": GOOD_URL, "passcode": "ax72"})
    assert r.status_code == 200
    assert r.json()["is_series"] is False
    assert svc.inspect_calls == [{"url": GOOD_URL, "passcode": "ax72"}]


def test_inspect_provider_error(monkeypatch):
    """分享失效/提取码错误等解析失败 → 400 带原因。"""
    patch_inspect(monkeypatch, error="分享不存在")
    r = client.post("/api/candidates/inspect", headers=AUTH,
                    json={"share_url": GOOD_URL})
    assert r.status_code == 400 and "分享解析失败" in r.json()["detail"]


async def test_inspect_share_parses_files_and_seasons(tmp_db, monkeypatch):
    """inspect_share 实际逻辑：过滤非视频/垃圾文件，解析季集/规格，分季聚合。"""
    from app import candidate_service as cs_mod
    from app.providers.base import ShareFile

    class FakeProvider:
        def __init__(self, cookie=None, db_path=None):
            pass

        async def get_share_stoken(self, pwd_id, passcode=""):
            return "st"

        async def list_share_files(self, pwd_id, stoken):
            return [
                ShareFile(fid="f1", name="余罪.S01E01.2016.1080p.WEB-DL.H264.chs.mp4",
                          size=800_000_000, is_dir=False, fid_token="t1",
                          path="余罪 第一季/余罪.S01E01.mp4"),
                ShareFile(fid="f2", name="余罪.S01E02.2016.1080p.WEB-DL.H264.chs.mp4",
                          size=810_000_000, is_dir=False, fid_token="t2",
                          path="余罪 第一季/余罪.S01E02.mp4"),
                ShareFile(fid="f3", name="06.mkv", size=1, is_dir=False,
                          fid_token="t3", path="06.mkv"),
                ShareFile(fid="d0", name="海报", size=0, is_dir=True,
                          fid_token="t0", path="海报"),
                ShareFile(fid="f9", name="sample.mp4", size=5, is_dir=False,
                          fid_token="t9", path="sample.mp4"),
                ShareFile(fid="f8", name="说明.txt", size=5, is_dir=False,
                          fid_token="t8", path="说明.txt"),
            ]

        async def close(self):
            pass

    monkeypatch.setattr(cs_mod, "QuarkDriveProvider", FakeProvider)
    monkeypatch.setattr(cs_mod.db, "load_cookie", lambda p: "c=1")
    svc = cs_mod.CandidateService(db_path=tmp_db)
    data = await svc.inspect_share(GOOD_URL, "ax72")
    assert data["video_count"] == 3
    assert data["other_count"] == 2          # sample.mp4（垃圾）+ 说明.txt（非视频）
    assert data["is_series"] is True
    assert data["seasons"] == [{"season": 1, "count": 2, "size_h": "1.5GB"}]
    files = data["files"]
    assert [f["episode"] for f in files] == [1, 2, 6]   # 按季集排序
    assert files[0]["season"] == 1
    assert files[0]["spec"]["resolution"] == "1080p"
    assert files[2]["name"] == "06.mkv"      # 裸集数文件：season 为空、episode=6
    assert files[2]["season"] is None and files[2]["episode"] == 6
    assert data["total_size"] == 800_000_000 + 810_000_000 + 1
    assert data["total_size_h"].endswith("GB")


async def test_inspect_share_no_cookie_raises(tmp_db, monkeypatch):
    """未绑定网盘账号时 inspect 报错（由路由转 400）。"""
    from app import candidate_service as cs_mod

    monkeypatch.setattr(cs_mod.db, "load_cookie", lambda p: None)
    monkeypatch.setattr(cs_mod.config, "QUARK_COOKIE", "")
    svc = cs_mod.CandidateService(db_path=tmp_db)
    try:
        await svc.inspect_share(GOOD_URL)
        assert False, "应当抛出 ProviderError"
    except cs_mod.ProviderError as e:
        assert "授权" in str(e)


# ---------- 页面骨架（前后端分离：页面为 web/ 静态文件，后端提供静态服务） ----------

PAGES = ("/setup", "/search", "/candidates", "/task/some-id")


def test_pages_render():
    for path in PAGES:
        r = client.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert r.headers["cache-control"] == "no-store"  # 管理页禁止缓存
        assert r.text.rstrip().endswith("</html>")


def test_pages_script_tags_balanced():
    """回归：</script> 闭合标签丢失会让脚本不执行，页面永远停在加载中。"""
    for path in PAGES:
        page = client.get(path).text
        assert page.count("<script") == page.count("</script>") >= 1


def test_pages_reference_existing_static_assets():
    """页面引用的每个 /static/* 资源都必须真实存在且可服务（no-store）。"""
    for path in PAGES:
        page = client.get(path).text
        refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', page)
        assert refs, f"{path} 未引用任何静态资源"
        for ref in refs:
            r = client.get(ref)
            assert r.status_code == 200, f"{path} 引用的 {ref} 不可访问"
            assert r.headers["cache-control"] == "no-store"


def test_static_js_contains_no_python_artifacts():
    """回归：JS 独立成文件后不应残留 Python f-string 花括号等占位符。"""
    js = client.get("/static/js/common.js").text
    assert "TOKEN_KEY" in js and "function api" in js
    for page_path in PAGES:
        page = client.get(page_path).text
        assert "{title}" not in page  # 服务端模板占位符不应出现在静态页


def test_setup_submit_rejects_bad_input(tmp_db, monkeypatch):
    """POST /setup 返回 JSON 契约（{ok, message}）：令牌错误 403、Cookie 不完整 400。"""
    r = client.post("/setup", data={"token": "wrong", "cookie": "a=1; __puus=x"})
    assert r.status_code == 403 and r.json()["ok"] is False and "令牌" in r.json()["message"]
    r = client.post("/setup", data={"token": "test-admin-token", "cookie": "kps=1"})
    assert r.status_code == 400 and "__puus" in r.json()["message"]


def test_setup_submit_ok_saves_cookie(tmp_db, monkeypatch):
    from app import web_routes as wr

    class FakeProvider:
        def __init__(self, cookie=None, db_path=None):
            self.cookie = cookie

        async def verify(self):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(wr, "QuarkDriveProvider", FakeProvider)
    monkeypatch.setattr(wr.config, "DB_PATH", tmp_db)
    r = client.post("/setup", data={"token": "test-admin-token",
                                    "cookie": '  "kps=1; __puus=x"  '})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert db.load_cookie(tmp_db) == "kps=1; __puus=x"  # 引号与空白已剥离


# ---------- 阶段 7：搜索缓存（1 天） ----------

async def test_search_result_cached_one_day(tmp_db, monkeypatch):
    """同参数第二次搜索命中 kv_cache，不再请求外部来源；候选内容一致。"""
    calls = patch_real_search(monkeypatch, tmp_db, "余罪", "Yu Zui")
    svc = CandidateService(db_path=tmp_db)
    m1, c1, e1, kw1 = await svc.search("series", "tt5925192")
    assert len(calls) == 2                      # 中英名各实搜一次
    m2, c2, e2, kw2 = await svc.search("series", "tt5925192")
    assert len(calls) == 2                      # 未新增来源请求
    assert [c.share_url for c in c2] == [c.share_url for c in c1]
    assert kw2 == kw1 and e2 == ""
    assert (m2.id if m2 else None) == (m1.id if m1 else None)


async def test_search_cache_disabled_when_ttl_zero(tmp_db, monkeypatch):
    """SEARCH_CACHE_TTL=0：关闭缓存，每次都实搜。"""
    calls = patch_real_search(monkeypatch, tmp_db, "余罪", "Yu Zui")
    monkeypatch.setattr(config, "SEARCH_CACHE_TTL", 0)
    svc = CandidateService(db_path=tmp_db)
    await svc.search("series", "tt5925192")
    await svc.search("series", "tt5925192")
    assert len(calls) == 4                      # 两次都实搜


async def test_search_cache_distinguishes_disks(tmp_db, monkeypatch):
    """不同 disks 参数 → 不同缓存键（默认夸克 ≠ 全部网盘）。"""
    calls = patch_real_search(monkeypatch, tmp_db, "FRIENDS")
    svc = CandidateService(db_path=tmp_db)
    await svc.search("series", "tt5925192", disks=None)
    await svc.search("series", "tt5925192", disks=[])
    assert len(calls) == 2                      # 缓存键不同，各实搜一轮


# ---------- 阶段 7：内容详情直显 + 无视频文件排除 ----------

async def test_search_marks_no_video_content_bad(tmp_db, monkeypatch):
    """有效链接但内容里没有任何视频文件 → 标记 bad（"内容无视频文件"）。"""
    from app import candidate_service as cs_mod
    from app.providers.base import ShareFile

    patch_real_search(monkeypatch, tmp_db, "FRIENDS")
    db.save_cookie(tmp_db, "c=1")

    class FakeProvider:
        def __init__(self, cookie=None, db_path=None):
            pass

        async def get_share_stoken(self, pwd_id, passcode=""):
            return "st"

        async def list_share_files(self, pwd_id, stoken):
            return [ShareFile(fid="f1", name="说明.txt", size=5, is_dir=False,
                              fid_token="t1", path="说明.txt")]

        async def close(self):
            pass

    monkeypatch.setattr(cs_mod, "QuarkDriveProvider", FakeProvider)
    svc = cs_mod.CandidateService(db_path=tmp_db)
    m, cands, errs, kw = await svc.search("series", "tt5925192")
    target = next(c for c in cands if c.share_url == URL_A)
    assert target.alive_state == "bad"           # 从"有效"中排除
    assert target.alive_summary == "内容无视频文件"
    assert target.detail is not None
    ok_any = [c for c in cands if c.alive_state == "ok"]
    assert all(c.share_url != URL_A for c in ok_any)


async def test_inspect_share_cached_one_day(tmp_db, monkeypatch):
    """inspect 结果缓存：同链接第二次解析不再请求网盘。"""
    from app import candidate_service as cs_mod
    from app.providers.base import ShareFile

    db.save_cookie(tmp_db, "c=1")
    calls = {"n": 0}

    class FakeProvider:
        def __init__(self, cookie=None, db_path=None):
            pass

        async def get_share_stoken(self, pwd_id, passcode=""):
            calls["n"] += 1
            return "st"

        async def list_share_files(self, pwd_id, stoken):
            return [ShareFile(fid="f1", name="A.S01E01.mp4", size=100,
                              is_dir=False, fid_token="t1", path="A.S01E01.mp4")]

        async def close(self):
            pass

    monkeypatch.setattr(cs_mod, "QuarkDriveProvider", FakeProvider)
    svc = cs_mod.CandidateService(db_path=tmp_db)
    d1 = await svc.inspect_share(GOOD_URL)
    d2 = await svc.inspect_share(GOOD_URL)
    assert calls["n"] == 1                       # 第二次走缓存
    assert d1 == d2 and d1["video_count"] == 1


# ---------- 阶段 7：picks 多选转存 ----------

def test_confirm_picks_mode(tmp_db, monkeypatch):
    """picks 模式：fids 必填；fid 列表（排序后 JSON）参与幂等键，与 whole 并存。"""
    patch_fetch_meta(monkeypatch)
    # 缺 fids → 400
    r = _confirm(mode="picks")
    assert r.status_code == 400 and "fids" in r.json()["detail"]
    r = _confirm(mode="picks", fids=[])
    assert r.status_code == 400
    # 正常创建：任务不锚定季集，picks 存排序后 JSON
    r = _confirm(mode="picks", fids=["b" * 32, "a" * 32])
    assert r.status_code == 200 and r.json()["created"] is True
    task = db.get_task(tmp_db, r.json()["task_id"])
    assert task["mode"] == "picks"
    assert task["season"] is None and task["episode"] is None
    assert json.loads(task["picks"]) == sorted(["a" * 32, "b" * 32])
    # 幂等：同 fids（顺序无关）复用活跃任务
    r2 = _confirm(mode="picks", fids=["a" * 32, "b" * 32])
    assert r2.json()["created"] is False and r2.json()["task_id"] == task["id"]
    # 不同 fids → 新任务
    r3 = _confirm(mode="picks", fids=["a" * 32, "c" * 32])
    assert r3.json()["created"] is True and r3.json()["task_id"] != task["id"]
    # 与 whole / episode 任务可并存
    assert _confirm(mode="whole").json()["created"] is True
    assert _confirm().json()["created"] is True


def test_task_api_includes_save_dir(tmp_db, monkeypatch):
    patch_fetch_meta(monkeypatch)
    task_id = _confirm().json()["task_id"]
    r = client.get(f"/api/task/{task_id}", headers=AUTH)
    assert r.json()["save_dir"] == config.TRANSFER_DIR


# ---------- 阶段 7：产品名称注入（GoddamnCool!） ----------

def test_pages_app_name_injected():
    for path in PAGES:
        page = client.get(path).text
        assert config.APP_NAME in page            # 标题/品牌已注入
        assert "{{APP_NAME" not in page           # 占位符已全部替换


def test_manifest_name_is_app_name():
    r = client.get("/manifest.json")
    assert r.status_code == 200
    assert r.json()["name"] == config.APP_NAME

