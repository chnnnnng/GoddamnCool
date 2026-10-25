"""Stremio 协议端点测试：manifest / stream 全链路（mock 网盘与 TMDB）。"""
import json
import os

# 必须在导入 app 之前设置（startup 校验）
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DB_PATH", "/tmp/quark_stremio_test.db")

from fastapi.testclient import TestClient  # noqa: E402

import app.stream_service as ss  # noqa: E402
import app.stremio_routes as stremio_routes  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.base import CloudDriveProvider, CloudFile, PlayResource  # noqa: E402
from app.stream_service import parse_video_id  # noqa: E402

client = TestClient(app)
client.__enter__()  # 触发 lifespan（db 初始化）


class FakeProvider(CloudDriveProvider):
    name = "夸克网盘"

    def __init__(self, files):
        self.files = files
        self.requested_play = None

    async def verify(self):
        return None

    async def search_files(self, keyword, limit=50):
        kw = keyword.lower().replace(" ", ".")
        return [f for f in self.files if kw in f.name.lower()]

    async def get_play_resource(self, file: CloudFile) -> PlayResource:
        self.requested_play = file
        return PlayResource(url="https://dl.example/x.mp4", headers={"Cookie": "c=1"},
                            fid=file.fid, filename=file.name, video_size=file.size,
                            source_name=self.name)

    async def close(self):
        pass


def patch_service(monkeypatch, provider, meta_dict):
    """构造测试用 StreamService：db_path 用真实初始化的临时库
    （:memory: 每次连接都是空库，新阶段的 db 查询会报 no such table）。

    同时建测试用户并绑定网盘 Cookie（每用户 Add-on 端点 /s/{token}/... 用），
    svc.user_token 保存该用户的 stream_token。"""
    import uuid

    from app import db as db_mod
    from app.auth import hash_password, new_stream_token
    from app.config import SECRET_KEY

    path = f"/tmp/qs_streamsvc_{uuid.uuid4().hex}.db"
    db_mod.init(path, SECRET_KEY)
    user = db_mod.create_user(path, "u@test.io", hash_password("password123"),
                              "user", "pro", new_stream_token())
    db_mod.save_user_cookie(path, user["id"], "cookie")
    monkeypatch.setattr(stremio_routes.config, "DB_PATH", path)  # 用户端点读此库
    svc = ss.StreamService(db_path=path, threshold=0.6)
    svc.user_token = user["stream_token"]
    monkeypatch.setattr(svc, "create_provider",
                        lambda cookie, user_id="": provider)

    async def fake_fetch_meta(*a, **k):
        return _meta(meta_dict)

    monkeypatch.setattr(ss, "fetch_meta", fake_fetch_meta)
    return svc


def _meta(d):
    from app.meta.base import VideoMeta
    return VideoMeta(d)


# ---------- parse_video_id ----------

def test_parse_video_id():
    assert parse_video_id("tt0848228") == ("tt0848228", None, None)
    assert parse_video_id("tt5636724:1:12") == ("tt5636724", 1, 12)


# ---------- 转存任务定位短路（阶段 4） ----------

async def test_stream_trusts_task_located_file(monkeypatch):
    """裸集数文件名（06.mkv）标题匹配必失败，但 COMPLETED 任务的
    target_fid 已精准定位 → 直接返回播放流（阶段 4 播放闭环）。"""
    bare = CloudFile(fid="fid-bare", name="06.mkv", size=800, is_dir=False)
    provider = FakeProvider([bare])
    svc = patch_service(monkeypatch, provider,
                        {"imdb_id": "tt5925192", "name": "余罪",
                         "type": "series", "releaseInfo": "2016"})
    from app import db as db_mod
    db_mod.create_task(svc.db_path, vtype="series", imdb_id="tt5925192",
                       season=2, episode=6, title="余罪", candidate_title="余罪S2",
                       share_url="https://pan.quark.cn/s/abc123def")
    from app import db as _db
    task = _db.get_tasks_for_video(svc.db_path, "series", "tt5925192", 2, 6)[0]
    _db.update_task_status(svc.db_path, task["id"], "COMPLETED")
    # 任务定位信息（result_json.name 会让 _search_all 顺带搜索裸集数文件名）
    _db.set_task_fields(svc.db_path, task["id"], target_fid="fid-bare",
                        result_json='{"name": "06.mkv"}')

    streams, _ = await svc.get_streams("series", "tt5925192:2:6", cookie="cookie")
    assert len(streams) == 1
    s = streams[0]
    assert s["url"].startswith("https://dl.example/")  # 直链模式
    assert s["description"].startswith("转存任务已定位该文件")
    assert s["behaviorHints"]["filename"] == "06.mkv"
    assert provider.requested_play.fid == "fid-bare"


async def test_stream_guidance_shows_task_progress(monkeypatch):
    """转存进行中：引导流指向任务页并提示进度，而非候选页。"""
    provider = FakeProvider([])  # 网盘搜索无结果
    svc = patch_service(monkeypatch, provider,
                        {"imdb_id": "tt5925192", "name": "余罪",
                         "type": "series", "releaseInfo": "2016"})
    from app import db as db_mod
    task, _ = db_mod.create_task(svc.db_path, vtype="series", imdb_id="tt5925192",
                                 season=1, episode=1, title="余罪",
                                 candidate_title="余罪S1", share_url="https://pan.quark.cn/s/x1")
    db_mod.claim_task(svc.db_path, task["id"], "PENDING", "PROCESSING")

    streams, _ = await svc.get_streams("series", "tt5925192:1:1", cookie="cookie")
    assert len(streams) == 1
    s = streams[0]
    assert s["externalUrl"].endswith(f"/task/{task['id']}")
    assert "转存" in s["description"]


# ---------- 整包任务播放闭环（阶段 5） ----------

def _create_whole_task(svc, files: list[dict]):
    from app import db as db_mod
    task, _ = db_mod.create_task(svc.db_path, vtype="series", imdb_id="tt5925192",
                                 season=None, episode=None, title="余罪",
                                 candidate_title="余罪 整包",
                                 share_url="https://pan.quark.cn/s/abc123def",
                                 mode="whole")
    db_mod.update_task_status(svc.db_path, task["id"], "COMPLETED")
    db_mod.set_task_fields(svc.db_path, task["id"],
                           result_json=json.dumps({"mode": "bulk", "files": files},
                                                  ensure_ascii=False))
    return task


async def test_stream_whole_task_locates_bare_episode_file(monkeypatch):
    """整包任务落盘清单按集数短路：裸集数文件名 06.mkv 标题匹配必失败，
    必须靠 result_json.files 解析出的集数定位（阶段 5 闭环）。"""
    e5 = CloudFile(fid="fid-e5", name="05.mkv", size=800, is_dir=False)
    e6 = CloudFile(fid="fid-e6", name="06.mkv", size=800, is_dir=False)
    provider = FakeProvider([e5, e6])
    svc = patch_service(monkeypatch, provider,
                        {"imdb_id": "tt5925192", "name": "余罪",
                         "type": "series", "releaseInfo": "2016"})
    task = _create_whole_task(svc, [
        {"name": "05.mkv", "fid": "fid-e5", "size": 800},
        {"name": "06.mkv", "fid": "fid-e6", "size": 800},
    ])

    streams, _ = await svc.get_streams("series", "tt5925192:2:6", cookie="cookie")
    assert len(streams) == 1
    s = streams[0]
    assert s["description"].startswith("转存任务已定位该文件")
    assert s["behaviorHints"]["filename"] == "06.mkv"
    assert provider.requested_play.fid == "fid-e6"
    assert task["mode"] == "whole"


async def test_stream_whole_task_missing_episode_no_shortcircuit(monkeypatch):
    """整包任务落盘清单中无目标集数 → 不短路，返回任务页引导流。"""
    provider = FakeProvider([])  # 网盘无匹配文件
    svc = patch_service(monkeypatch, provider,
                        {"imdb_id": "tt5925192", "name": "余罪",
                         "type": "series", "releaseInfo": "2016"})
    task = _create_whole_task(svc, [{"name": "05.mkv", "fid": "fid-e5", "size": 800}])

    streams, _ = await svc.get_streams("series", "tt5925192:2:6", cookie="cookie")
    assert len(streams) == 1
    assert streams[0]["externalUrl"].endswith(f"/task/{task['id']}")


async def test_stream_whole_task_season_guard(monkeypatch):
    """落盘文件名明确标注了其他季（S02E05）而请求 S1 → 不短路，避免错季播放。"""
    wrong = CloudFile(fid="fid-x", name="Show.S02E05.2024.1080p.mkv", size=800,
                      is_dir=False)
    provider = FakeProvider([wrong])
    svc = patch_service(monkeypatch, provider,
                        {"imdb_id": "tt5925192", "name": "余罪",
                         "type": "series", "releaseInfo": "2016"})
    task = _create_whole_task(svc, [{"name": "Show.S02E05.2024.1080p.mkv",
                                     "fid": "fid-x", "size": 800}])

    streams, _ = await svc.get_streams("series", "tt5925192:1:5", cookie="cookie")
    assert len(streams) == 1
    assert streams[0]["externalUrl"].endswith(f"/task/{task['id']}")


# ---------- manifest（每用户 Add-on） ----------

def test_manifest_schema(monkeypatch):
    svc = patch_service(monkeypatch, FakeProvider([]),
                        {"imdb_id": "tt0848228", "name": "The Avengers",
                         "type": "movie", "releaseInfo": "2012"})
    r = client.get(f"/s/{svc.user_token}/manifest.json")
    assert r.status_code == 200
    m = r.json()
    for key in ("id", "version", "name", "description", "resources", "types", "idPrefixes"):
        assert key in m
    assert m["resources"] == ["stream"]
    assert m["idPrefixes"] == ["tt"]
    assert "movie" in m["types"] and "series" in m["types"]
    assert "catalogs" in m


def test_manifest_invalid_token_404():
    assert client.get("/s/deadbeef/manifest.json").status_code == 404


# ---------- stream（每用户 Add-on） ----------

def test_stream_movie_hit(monkeypatch):
    files = [CloudFile(fid="f1", name="The.Avengers.2012.1080p.BluRay.x264.mkv",
                       size=2048, is_dir=False)]
    svc = patch_service(monkeypatch, FakeProvider(files),
                        {"imdb_id": "tt0848228", "name": "The Avengers",
                         "type": "movie", "releaseInfo": "2012"})
    monkeypatch.setattr(stremio_routes, "service", svc)

    r = client.get(f"/s/{svc.user_token}/stream/movie/tt0848228.json")
    assert r.status_code == 200
    data = r.json()
    assert set(data.keys()) == {"streams"}
    s = data["streams"][0]
    assert s["url"] == "https://dl.example/x.mp4"  # 直链 + proxyHeaders
    assert s["behaviorHints"]["notWebReady"] is True
    assert s["behaviorHints"]["proxyHeaders"]["request"] == {"Cookie": "c=1"}
    assert s["behaviorHints"]["filename"].endswith(".mkv")


def test_stream_hit_counts_play(monkeypatch):
    """命中可用流时播放计次 +1；/api/auth/me 回显播放统计。"""
    from app import db as db_mod
    files = [CloudFile(fid="f1", name="The.Avengers.2012.1080p.BluRay.x264.mkv",
                       size=2048, is_dir=False)]
    svc = patch_service(monkeypatch, FakeProvider(files),
                        {"imdb_id": "tt0848228", "name": "The Avengers",
                         "type": "movie", "releaseInfo": "2012"})
    monkeypatch.setattr(stremio_routes, "service", svc)
    uid = db_mod.get_user_by_stream_token(svc.db_path, svc.user_token)["id"]
    client.get(f"/s/{svc.user_token}/stream/movie/tt0848228.json")
    client.get(f"/s/{svc.user_token}/stream/movie/tt0848228.json")
    assert db_mod.get_play_count(svc.db_path, uid) == 2
    assert db_mod.get_play_total(svc.db_path, uid) == 2


def test_stream_no_cookie_guides_panel(monkeypatch):
    """未绑定 Cookie：严格不回退，返回 /panel 绑定引导流。"""
    svc = patch_service(monkeypatch, FakeProvider([]),
                        {"imdb_id": "tt0848228", "name": "The Avengers",
                         "type": "movie", "releaseInfo": "2012"})
    from app import db as db_mod
    uid = db_mod.get_user_by_stream_token(svc.db_path, svc.user_token)["id"]
    db_mod.delete_user_cookie(svc.db_path, uid)
    monkeypatch.setattr(stremio_routes, "service", svc)
    r = client.get(f"/s/{svc.user_token}/stream/movie/tt0848228.json")
    streams = r.json()["streams"]
    assert len(streams) == 1
    assert streams[0]["externalUrl"].endswith("/panel")
    assert "url" not in streams[0]


def test_stream_invalid_token(monkeypatch):
    svc = patch_service(monkeypatch, FakeProvider([]),
                        {"imdb_id": "tt0848228", "name": "The Avengers",
                         "type": "movie", "releaseInfo": "2012"})
    monkeypatch.setattr(stremio_routes, "service", svc)
    r = client.get("/s/no-such-token/stream/movie/tt0848228.json")
    assert r.status_code == 200 and r.json()["streams"] == []


def test_stream_low_confidence_guidance(monkeypatch):
    files = [CloudFile(fid="f1", name="Unrelated.Show.2020.1080p.mkv", size=1, is_dir=False)]
    svc = patch_service(monkeypatch, FakeProvider(files),
                        {"imdb_id": "tt0848228", "name": "The Avengers",
                         "type": "movie", "releaseInfo": "2012"})
    monkeypatch.setattr(stremio_routes, "service", svc)
    r = client.get(f"/s/{svc.user_token}/stream/movie/tt0848228.json")
    streams = r.json()["streams"]
    # 未命中时返回 externalUrl 引导流（候选资源页），不伪造播放流
    assert len(streams) == 1
    g = streams[0]
    assert g["externalUrl"] == "http://addon.example.test/search?vtype=movie&id=tt0848228"
    assert "url" not in g
    assert r.headers.get("X-Addon-Status")


def test_stream_series_episode(monkeypatch):
    files = [CloudFile(fid="f12", name="余罪.S01E12.2016.1080p.WEB-DL.mp4", size=1, is_dir=False)]
    svc = patch_service(monkeypatch, FakeProvider(files),
                        {"imdb_id": "tt5636724", "name": "余罪",
                         "type": "series", "releaseInfo": "2016",
                         "videos": [{"season": 1, "episode": 12, "name": "一切归零"}]})
    monkeypatch.setattr(stremio_routes, "service", svc)
    r = client.get(f"/s/{svc.user_token}/stream/series/tt5636724:1:12.json")
    assert r.status_code == 200
    assert len(r.json()["streams"]) == 1


def test_stream_series_wrong_episode_guidance(monkeypatch):
    files = [CloudFile(fid="f11", name="余罪.S01E11.2016.1080p.mp4", size=1, is_dir=False)]
    svc = patch_service(monkeypatch, FakeProvider(files),
                        {"imdb_id": "tt5636724", "name": "余罪",
                         "type": "series", "releaseInfo": "2016",
                         "videos": [{"season": 1, "episode": 12, "name": "一切归零"}]})
    monkeypatch.setattr(stremio_routes, "service", svc)
    r = client.get(f"/s/{svc.user_token}/stream/series/tt5636724:1:12.json")
    streams = r.json()["streams"]
    assert len(streams) == 1
    assert streams[0]["externalUrl"].endswith("/search?vtype=series&id=tt5636724:1:12")


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------- 流结果规格 emoji 行（需求第 11 条） ----------

def test_spec_emoji_line():
    info = ss.name_parser.parse(
        "余罪.S01E01.2016.2160p.WEB-DL.H265.HDR.Atmos.中英双字.mp4")
    line = ss._spec_emoji_line(info)
    assert line == "🎬 2160p · WEB-DL · H.265 ✨ HDR 🎧 Atmos 💬 中英双字"


def test_spec_emoji_line_minimal():
    assert ss._spec_emoji_line(ss.name_parser.parse("余罪.S01E01.mp4")) == ""
    line = ss._spec_emoji_line(ss.name_parser.parse("Show.1080p.mkv"))
    assert line == "🎬 1080p"


# ---------- file_index 快路径（阶段新功能：O(1) 后续命中）----------

async def test_file_index_fast_path_after_matcher_hit(monkeypatch):
    """第一次 matcher 命中 → 写入 file_index；第二次同一请求 →
    直接走 file_index 快路径，完全不触发网盘搜索。"""
    files = [CloudFile(fid="f-movie", name="The.Avengers.2012.1080p.mkv",
                       size=2048, is_dir=False)]
    provider = FakeProvider(files)
    svc = patch_service(monkeypatch, provider,
                        {"imdb_id": "tt0848228", "name": "The Avengers",
                         "type": "movie", "releaseInfo": "2012"})
    search_count_before = provider.search_files  # FakeProvider 无计数，用 fid 代

    # 第一次：走 matcher 路径
    streams1, status1 = await svc.get_streams("movie", "tt0848228", cookie="cookie")
    assert len(streams1) == 1
    assert status1.startswith("标题相似度")

    # 验证 file_index 已写入
    from app import db as db_mod
    idx = db_mod.get_file_index(svc.db_path, user_id="",
                                imdb_id="tt0848228", vtype="movie",
                                season=None, episode=None)
    assert idx is not None
    assert idx["fid"] == "f-movie"
    assert idx["filename"] == "The.Avengers.2012.1080p.mkv"
    assert idx["source"] == "matcher"

    # 第二次：file_index 快路径 → FakeProvider.search_files 不会被调
    # （但 FakeProvider.search_files 没抛异常，所以我们用 monkeypatch 记录调用次数）
    provider.search_files.__func__
    call_log = {"n": 0}
    orig_search = provider.search_files

    async def counting_search(*a, **k):
        call_log["n"] += 1
        return await orig_search(*a, **k)

    provider.search_files = counting_search

    streams2, status2 = await svc.get_streams("movie", "tt0848228", cookie="cookie")
    assert len(streams2) == 1
    assert status2 == "file_index 快路径命中"
    # 快路径不触发网盘搜索（但我们的 get_play_resource 仍然会被调一次）
    assert call_log["n"] == 0

    # 第三次：已在 file_index，依然快路径
    idx_hit = db_mod.get_file_index(svc.db_path, user_id="",
                                    imdb_id="tt0848228", vtype="movie",
                                    season=None, episode=None)
    assert idx_hit is not None  # 已存在


async def test_file_index_written_on_task_path(monkeypatch):
    """从 transfer_task 直接路径命中后也写 file_index，后续同一请求 O(1) 命中。"""
    provider = FakeProvider([])  # 网盘什么都搜不到（确认不会走 matcher 路径）
    svc = patch_service(monkeypatch, provider,
                        {"imdb_id": "tt0848228", "name": "The Avengers",
                         "type": "movie", "releaseInfo": "2012"})
    from app import db as db_mod
    db_mod.create_task(svc.db_path, vtype="movie", imdb_id="tt0848228",
                       season=None, episode=None, title="The Avengers",
                       candidate_title="The Avengers",
                       share_url="https://pan.quark.cn/s/x",
                       mode="episode")
    t = db_mod.get_tasks_for_video(svc.db_path, "movie", "tt0848228", None, None)[0]
    db_mod.update_task_status(svc.db_path, t["id"], "COMPLETED")
    db_mod.set_task_fields(svc.db_path, t["id"], target_fid="f-target",
                           result_json='{"name":"The.Avengers.2012.mkv"}')

    # 第一次：transfer_task 直接路径
    s1, status1 = await svc.get_streams("movie", "tt0848228", cookie="cookie")
    assert len(s1) == 1
    assert status1 == "转存任务已定位该文件"

    # file_index 已写入
    idx = db_mod.get_file_index(svc.db_path, user_id="",
                                imdb_id="tt0848228", vtype="movie",
                                season=None, episode=None)
    assert idx is not None
    assert idx["fid"] == "f-target"
    assert idx["source"] == "task"

    # 第二次：file_index 快路径
    s2, status2 = await svc.get_streams("movie", "tt0848228", cookie="cookie")
    assert len(s2) == 1
    assert status2 == "file_index 快路径命中"


async def test_file_index_fid_stale_falls_back(monkeypatch):
    """file_index 命中但 fid 已失效（Cookie 变更）→ ProviderError →
    自动 fallback 到 transfer_task / matcher 路径，不卡死。"""
    files = [CloudFile(fid="f-live", name="Show.2024.mkv", size=100, is_dir=False)]
    provider = FakeProvider(files)
    svc = patch_service(monkeypatch, provider,
                        {"imdb_id": "tt0848228", "name": "Show",
                         "type": "movie", "releaseInfo": "2024"})
    from app import db as db_mod

    # 预先塞一个过期 fid
    db_mod.upsert_file_index(svc.db_path, user_id="", imdb_id="tt0848228",
                             vtype="movie", season=None, episode=None,
                             fid="f-dead", filename="dead.mkv", source="matcher")

    # 让 get_play_resource 对 f-dead 抛 ProviderError，对 f-live 正常
    orig_play = provider.get_play_resource

    async def smart_play(file):
        if file.fid == "f-dead":
            from app.providers.base import ProviderError
            raise ProviderError("fid expired")
        return await orig_play(file)

    provider.get_play_resource = smart_play

    s, status = await svc.get_streams("movie", "tt0848228", cookie="cookie")
    # 应该 fallback 到 matcher 路径命中 f-live
    assert len(s) == 1
    assert status.startswith("标题相似度")


async def test_delete_file_index_on_cookie_reset(monkeypatch):
    """delete_file_index_for_user 清掉某用户全部快路径缓存。"""
    from app import db as db_mod
    svc = patch_service(monkeypatch, FakeProvider([]),
                        {"imdb_id": "tt0848228", "name": "Show",
                         "type": "movie", "releaseInfo": "2024"})
    db_mod.upsert_file_index(svc.db_path, user_id="u1", imdb_id="tt0848228",
                             vtype="movie", season=None, episode=None,
                             fid="f1", filename="s.mkv", source="matcher")
    db_mod.upsert_file_index(svc.db_path, user_id="u2", imdb_id="tt0848228",
                             vtype="movie", season=None, episode=None,
                             fid="f2", filename="s.mkv", source="matcher")
    # SQLite 没 count(*) 直接查，用 delete 结果数
    n = db_mod.delete_file_index_for_user(svc.db_path, "u1")
    assert n >= 1
    # u2 还在
    assert db_mod.get_file_index(svc.db_path, user_id="u2",
                                 imdb_id="tt0848228", vtype="movie",
                                 season=None, episode=None) is not None
    # u1 已清
    assert db_mod.get_file_index(svc.db_path, user_id="u1",
                                 imdb_id="tt0848228", vtype="movie",
                                 season=None, episode=None) is None


async def test_transfer_task_direct_path_bypasses_search(monkeypatch):
    """transfer_task 直接路径完全不调网盘搜索（验证架构改造核心收益）。"""
    provider = FakeProvider([])
    call_log = {"n": 0}
    orig_search = provider.search_files

    async def counting(*a, **k):
        call_log["n"] += 1
        return await orig_search(*a, **k)

    provider.search_files = counting
    svc = patch_service(monkeypatch, provider,
                        {"imdb_id": "tt5925192", "name": "余罪",
                         "type": "series", "releaseInfo": "2016"})
    from app import db as db_mod
    db_mod.create_task(svc.db_path, vtype="series", imdb_id="tt5925192",
                       season=1, episode=1, title="余罪",
                       candidate_title="余罪S1",
                       share_url="https://pan.quark.cn/s/x",
                       mode="episode")
    t = db_mod.get_tasks_for_video(svc.db_path, "series", "tt5925192", 1, 1)[0]
    db_mod.update_task_status(svc.db_path, t["id"], "COMPLETED")
    db_mod.set_task_fields(svc.db_path, t["id"], target_fid="f-direct",
                           result_json='{"name":"Yu.Zui.S01E01.2016.mkv"}')

    s, status = await svc.get_streams("series", "tt5925192:1:1", cookie="cookie")
    assert len(s) == 1
    assert status == "转存任务已定位该文件"
    # 关键验证：完全没调网盘搜索
    assert call_log["n"] == 0
