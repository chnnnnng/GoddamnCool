"""QuarkDriveProvider 测试：使用 httpx.MockTransport 模拟夸克接口。"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from httpx import AsyncClient, MockTransport, Response, Request  # noqa: E402

from app import db  # noqa: E402
from app.providers.base import CloudFile, ProviderAuthError  # noqa: E402
from app.providers.quark import QuarkDriveProvider  # noqa: E402

SECRET = Fernet.generate_key().decode()


@pytest.fixture()
def tmp_db(tmp_path):
    path = str(tmp_path / "t.db")
    db.init(path, SECRET)
    return path


def make_provider(cookie: str, handler, tmp_db, user_id: str = ""):
    p = QuarkDriveProvider(cookie=cookie, db_path=tmp_db, user_id=user_id)
    p._client = AsyncClient(
        transport=MockTransport(handler), timeout=10,
        headers={"Cookie": cookie, "Referer": "https://pan.quark.cn/",
                 "User-Agent": "ua"}, params={"pr": "ucpro", "fr": "pc"})
    return p


def api_json(code=0, data=None, message="ok"):
    return {"code": code, "message": message, "data": data}


async def test_search_and_puus_absorb(tmp_db):
    calls = []

    def handler(request: Request) -> Response:
        calls.append(str(request.url))
        assert "__puus=old-puus" in request.headers["cookie"]
        return Response(200, json=api_json(data={"list": [
            {"fid": "f1", "file_name": "余罪.S01E12.2016.1080p.mp4", "size": 831,
             "dir": False, "parent_path": "/0000temp"},
            {"fid": "d1", "file_name": "目录", "size": 0, "dir": True},
        ]}), headers={"Set-Cookie": "__puus=new-puus; path=/; domain=.quark.cn"})

    p = make_provider("kps=a; sign=b; __puus=old-puus", handler, tmp_db,
                      user_id="u1")
    try:
        files = await p.search_files("余罪")
        assert len(files) == 1 and files[0].fid == "f1" and not files[0].is_dir
        assert p._cookie.endswith("__puus=new-puus")
        assert "old-puus" not in p._cookie
        # __puus 已回写该用户的加密凭证
        assert db.load_user_cookie(tmp_db, "u1").endswith("__puus=new-puus")
    finally:
        await p.close()


async def test_play_url_prefers_original(tmp_db):
    """首选 /file/v2/play：取 trans_status=raw 的原画条目，不再调下载接口。"""
    called = []

    def handler(request: Request) -> Response:
        called.append(request.url.path)
        assert request.url.path.endswith("/file/v2/play")
        import json
        assert json.loads(request.content) == {"fid": "f1"}
        return Response(200, json=api_json(data={
            "size": 871483927,
            "video_list": [
                {"resolution": "super", "accessable": True, "trans_status": "raw",
                 "video_info": {"url": "https://video-play-c.example/raw.mp4?auth_key=1",
                                "size": 871483927, "width": 1920}},
                {"resolution": "low", "accessable": True, "trans_status": "success",
                 "video_info": {"url": "https://video-play-c.example/low.mp4?auth_key=1",
                                "size": 187, "width": 480}},
            ]}))

    p = make_provider("c=1", handler, tmp_db)
    try:
        res = await p.get_play_resource(CloudFile(fid="f1", name="a.mp4", size=871483927, is_dir=False))
        assert res.url == "https://video-play-c.example/raw.mp4?auth_key=1"
        assert res.headers["Referer"] == "https://pan.quark.cn/"
        assert called == ["/1/clouddrive/file/v2/play"]  # 未回退下载接口
    finally:
        await p.close()


async def test_play_fallback_to_download(tmp_db):
    """播放接口无条目时回退 /file/download。"""
    def handler(request: Request) -> Response:
        if request.url.path.endswith("/file/v2/play"):
            return Response(200, json=api_json(data={"size": 1, "video_list": []}))
        assert request.url.path.endswith("/file/download")
        return Response(200, json=api_json(
            data=[{"download_url": "https://dl.example/file.mp4?sig=1"}]))

    p = make_provider("c=1", handler, tmp_db)
    try:
        res = await p.get_play_resource(CloudFile(fid="f1", name="a.mp4", size=1, is_dir=False))
        assert res.url == "https://dl.example/file.mp4?sig=1"
    finally:
        await p.close()


async def test_play_fallback_on_error(tmp_db):
    """播放接口报错（如鉴权外的接口错误）时回退 /file/download。"""
    def handler(request: Request) -> Response:
        if request.url.path.endswith("/file/v2/play"):
            return Response(200, json=api_json(code=15000, message="inner error", data=None))
        return Response(200, json=api_json(
            data=[{"download_url": "https://dl.example/file.mp4?sig=2"}]))

    p = make_provider("c=1", handler, tmp_db)
    try:
        res = await p.get_play_resource(CloudFile(fid="f1", name="a.mp4", size=1, is_dir=False))
        assert res.url == "https://dl.example/file.mp4?sig=2"
    finally:
        await p.close()


async def test_play_skips_inaccessible_entries(tmp_db):
    """accessable=False 的条目（如非 SVIP 的原画）应跳过，取可访问的最高画质。"""
    def handler(request: Request) -> Response:
        return Response(200, json=api_json(data={
            "size": 1000,
            "video_list": [
                {"resolution": "super", "accessable": False, "trans_status": "raw",
                 "video_info": {"url": "https://x/raw", "size": 1000}},
                {"resolution": "high", "accessable": True, "trans_status": "success",
                 "video_info": {"url": "https://x/high", "size": 500}},
            ]}))

    p = make_provider("c=1", handler, tmp_db)
    try:
        res = await p.get_play_resource(CloudFile(fid="f1", name="a.mp4", size=1000, is_dir=False))
        assert res.url == "https://x/high"
    finally:
        await p.close()


async def test_auth_error(tmp_db):
    def handler(request: Request) -> Response:
        return Response(200, json=api_json(code=31001, message="token invalid"))

    p = make_provider("c=1", handler, tmp_db)
    try:
        with pytest.raises(ProviderAuthError):
            await p.verify()
    finally:
        await p.close()


async def test_http_412_is_auth_error(tmp_db):
    def handler(request: Request) -> Response:
        return Response(412, text="precondition failed")

    p = make_provider("c=1", handler, tmp_db)
    try:
        with pytest.raises(ProviderAuthError):
            await p.verify()
    finally:
        await p.close()


# ---------- 阶段 4：分享转存 ----------

from app.providers.base import ProviderError, ShareFile  # noqa: E402


async def test_share_stoken_invalid_share(tmp_db):
    """分享已失效（41011）应抛带语义的 ProviderError。"""
    def handler(request: Request) -> Response:
        assert request.url.path.endswith("/share/sharepage/token")
        return Response(200, json=api_json(code=41011, message="分享地址已失效"))

    p = make_provider("c=1", handler, tmp_db)
    try:
        with pytest.raises(ProviderError, match="分享"):
            await p.get_share_stoken("deadbeef")
    finally:
        await p.close()


async def test_share_stoken_http404_with_business_code(tmp_db):
    """实测：失效分享返回 HTTP 404 + body code=41011，应读出业务码而非抛 HTTPStatusError。"""
    def handler(request: Request) -> Response:
        return Response(404, json=api_json(code=41011, message="分享地址已失效"))

    p = make_provider("c=1", handler, tmp_db)
    try:
        with pytest.raises(ProviderError, match="分享"):
            await p.get_share_stoken("deadbeef")
    finally:
        await p.close()


async def test_save_share_files_parses_embedded_top_fids(tmp_db):
    """save 响应内嵌 task_resp.data.save_as.save_as_top_fids 应被解析出来。"""
    calls = []

    def handler(request: Request) -> Response:
        calls.append((request.url.path, request.read()))
        return Response(200, json={
            "code": 0, "message": "ok",
            "data": {"task_id": "qt-1", "task_sync": True,
                     "task_resp": {"status": 200, "code": 0, "data": {
                         "task_id": "qt-1", "status": 2,
                         "save_as": {"save_as_top_fids": ["new-fid-1"]}}}}})

    p = make_provider("c=1", handler, tmp_db)
    try:
        res = await p.save_share_files(
            "pwd1", "stoken-x", [ShareFile(fid="sf1", name="a.mkv", size=1,
                                           is_dir=False, fid_token="tok1")],
            to_pdir_fid="dir-9")
        assert res == {"task_id": "qt-1", "top_fids": ["new-fid-1"]}
        path, body = calls[0]
        assert path.endswith("/share/sharepage/save")
        import json
        b = json.loads(body)
        assert b["fid_list"] == ["sf1"] and b["fid_token_list"] == ["tok1"]
        assert b["to_pdir_fid"] == "dir-9" and b["scene"] == "link"
    finally:
        await p.close()


async def test_get_transfer_task_maps_status(tmp_db):
    def handler(request: Request) -> Response:
        assert request.url.params["task_id"] == "qt-1"
        assert request.url.params["retry_index"] == "3"
        return Response(200, json=api_json(data={
            "task_id": "qt-1", "status": 2,
            "save_as": {"save_as_top_fids": ["f1", "f2"]}}))

    p = make_provider("c=1", handler, tmp_db)
    try:
        info = await p.get_transfer_task("qt-1", retry_index=3)
        assert info == {"status": 2, "top_fids": ["f1", "f2"], "message": ""}
    finally:
        await p.close()


async def test_find_or_create_dir_conflict_rescan(tmp_db):
    """同名目录冲突（23008）时应重扫根目录取已存在的 fid。"""
    state = {"created": 0}

    def handler(request: Request) -> Response:
        path = request.url.path
        if path.endswith("/file/sort"):
            if state["created"]:
                return Response(200, json=api_json(data={"list": [
                    {"fid": "d-exist", "file_name": "GODDAMNCOOL", "dir": True}]}))
            return Response(200, json=api_json(data={"list": []}))
        assert path.endswith("/file") and request.method == "POST"
        state["created"] += 1
        return Response(200, json=api_json(code=23008, message="同名冲突"))

    p = make_provider("c=1", handler, tmp_db)
    try:
        assert await p.find_or_create_dir("GODDAMNCOOL") == "d-exist"
    finally:
        await p.close()
