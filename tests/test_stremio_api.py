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
    （:memory: 每次连接都是空库，新阶段的 db 查询会报 no such table）。"""
    import uuid

    from app import db as db_mod
    from app.config import SECRET_KEY

    path = f"/tmp/qs_streamsvc_{uuid.uuid4().hex}.db"
    db_mod.init(path, SECRET_KEY)
    svc = ss.StreamService(db_path=path, threshold=0.6)
    monkeypatch.setattr(svc, "load_cookie", lambda: "cookie")
    monkeypatch.setattr(svc, "create_provider", lambda cookie: provider)

    async def fake_fetch_meta(*a, **k):
        return _meta(meta_dict)

    monkeypatch.setattr(ss, "fetch_meta", fake_fetch_meta)
    return svc


def _meta(d):
    from app.tmdb import VideoMeta
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

    streams, _ = await svc.get_streams("series", "tt5925192:2:6")
    assert len(streams) == 1
    s = streams[0]
    assert s["url"].startswith("http")            # 测试环境为代理流，线上为直链
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

    streams, _ = await svc.get_streams("series", "tt5925192:1:1")
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

    streams, _ = await svc.get_streams("series", "tt5925192:2:6")
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

    streams, _ = await svc.get_streams("series", "tt5925192:2:6")
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

    streams, _ = await svc.get_streams("series", "tt5925192:1:5")
    assert len(streams) == 1
    assert streams[0]["externalUrl"].endswith(f"/task/{task['id']}")


# ---------- manifest ----------

def test_manifest_schema():
    r = client.get("/manifest.json")
    assert r.status_code == 200
    m = r.json()
    for key in ("id", "version", "name", "description", "resources", "types", "idPrefixes"):
        assert key in m
    assert m["resources"] == ["stream"]
    assert m["idPrefixes"] == ["tt"]
    assert "movie" in m["types"] and "series" in m["types"]
    assert "catalogs" in m


# ---------- stream ----------

def test_stream_movie_hit(monkeypatch):
    files = [CloudFile(fid="f1", name="The.Avengers.2012.1080p.BluRay.x264.mkv",
                       size=2048, is_dir=False)]
    svc = patch_service(monkeypatch, FakeProvider(files),
                        {"imdb_id": "tt0848228", "name": "The Avengers",
                         "type": "movie", "releaseInfo": "2012"})
    monkeypatch.setattr(stremio_routes, "service", svc)

    r = client.get("/stream/movie/tt0848228.json")
    assert r.status_code == 200
    data = r.json()
    assert set(data.keys()) == {"streams"}
    s = data["streams"][0]
    assert s["url"].startswith("http://addon.example.test/proxy/f1?")
    assert s["behaviorHints"]["filename"].endswith(".mkv")


def test_stream_no_cookie(monkeypatch):
    svc = ss.StreamService(db_path=":memory:")
    monkeypatch.setattr(svc, "load_cookie", lambda: None)
    monkeypatch.setattr(stremio_routes, "service", svc)
    r = client.get("/stream/movie/tt0848228.json")
    assert r.json() == {"streams": []}


def test_stream_low_confidence_guidance(monkeypatch):
    files = [CloudFile(fid="f1", name="Unrelated.Show.2020.1080p.mkv", size=1, is_dir=False)]
    svc = patch_service(monkeypatch, FakeProvider(files),
                        {"imdb_id": "tt0848228", "name": "The Avengers",
                         "type": "movie", "releaseInfo": "2012"})
    monkeypatch.setattr(stremio_routes, "service", svc)
    r = client.get("/stream/movie/tt0848228.json")
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
    r = client.get("/stream/series/tt5636724:1:12.json")
    assert r.status_code == 200
    assert len(r.json()["streams"]) == 1


def test_stream_series_wrong_episode_guidance(monkeypatch):
    files = [CloudFile(fid="f11", name="余罪.S01E11.2016.1080p.mp4", size=1, is_dir=False)]
    svc = patch_service(monkeypatch, FakeProvider(files),
                        {"imdb_id": "tt5636724", "name": "余罪",
                         "type": "series", "releaseInfo": "2016",
                         "videos": [{"season": 1, "episode": 12, "name": "一切归零"}]})
    monkeypatch.setattr(stremio_routes, "service", svc)
    r = client.get("/stream/series/tt5636724:1:12.json")
    streams = r.json()["streams"]
    assert len(streams) == 1
    assert streams[0]["externalUrl"].endswith("/search?vtype=series&id=tt5636724:1:12")


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
