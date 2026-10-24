"""PanSouProvider 测试：使用 httpx.MockTransport 模拟 PanSou /api/search。"""
import pytest
from httpx import AsyncClient, ConnectError, MockTransport, Response, Request

from app.providers.search import PanSouProvider, SearchCandidate, SearchError

PANSOU_BODY = {
    "code": 0, "message": "success",
    "data": {
        "total": 5,
        "merged_by_type": {
            "quark": [
                {"url": "https://pan.quark.cn/s/f32d0e563a19", "password": "",
                 "note": "余罪 第1-2季", "datetime": "0001-01-01T00:00:00Z",
                 "source": "plugin:wanou"},
                {"url": "https://pan.quark.cn/s/2a5efb0c3dc7", "password": "ax72",
                 "note": "【夸克网盘】余罪(1-2季)(2016)",
                 "datetime": "2026-09-23T21:26:13+08:00", "source": "plugin:mizixing"},
                # 脏数据：share id 拼接错误（实测存在）
                {"url": "https://pan.quark.cn/s/2a5efb0c3dc7BY", "password": "",
                 "note": "脏链接", "datetime": "2026-09-23T21:26:13+08:00",
                 "source": "plugin:x"},
                # 非夸克链接应被过滤
                {"url": "https://pan.baidu.com/s/1abcdef", "password": "y",
                 "note": "百度盘", "datetime": "", "source": "plugin:y"},
                # 与第 2 条重复的链接应去重
                {"url": "https://pan.quark.cn/s/2a5efb0c3dc7", "password": "ax72",
                 "note": "重复条目", "datetime": "2026-09-24T00:00:00+08:00",
                 "source": "plugin:z"},
            ],
        },
    },
}


def make_provider(handler, base="http://pansou.test"):
    p = PanSouProvider(base, timeout=10, retry_backoff=0)
    p._client = AsyncClient(transport=MockTransport(handler), timeout=10)
    return p


async def test_parse_filter_dedupe():
    def handler(request: Request) -> Response:
        assert request.url.path == "/api/search"
        assert request.method == "POST"
        body = request.read()
        assert b'"kw"' in body and b'"cloud_types":["quark"]' in body
        return Response(200, json=PANSOU_BODY)

    p = make_provider(handler)
    try:
        cands = await p.search("余罪")
    finally:
        await p.close()
    assert [c.share_url for c in cands] == [
        "https://pan.quark.cn/s/f32d0e563a19", "https://pan.quark.cn/s/2a5efb0c3dc7"]
    first, second = cands
    assert first.title == "余罪 第1-2季" and first.passcode == ""
    assert first.published == ""  # Go 零值时间视为未知
    assert first.source == "plugin:wanou" and first.provider == "PanSou"
    assert second.passcode == "ax72" and second.published.startswith("2026-09-23")


async def test_limit():
    def handler(request: Request) -> Response:
        return Response(200, json=PANSOU_BODY)

    p = make_provider(handler)
    try:
        assert len(await p.search("余罪", limit=1)) == 1
    finally:
        await p.close()


async def test_error_code_raises_search_error():
    def handler(request: Request) -> Response:
        return Response(200, json={"code": 500, "message": "inner error"})

    p = make_provider(handler)
    try:
        with pytest.raises(SearchError):
            await p.search("x")
    finally:
        await p.close()


async def test_http_5xx_raises_search_error():
    def handler(request: Request) -> Response:
        return Response(502, text="bad gateway")

    p = make_provider(handler)
    try:
        with pytest.raises(SearchError):
            await p.search("x")
    finally:
        await p.close()


async def test_non_json_raises_search_error():
    def handler(request: Request) -> Response:
        return Response(200, text="<html>not json</html>")

    p = make_provider(handler)
    try:
        with pytest.raises(SearchError):
            await p.search("x")
    finally:
        await p.close()


async def test_connection_error_raises_search_error():
    def handler(request: Request) -> Response:
        raise ConnectError("connection refused")

    p = make_provider(handler)
    try:
        with pytest.raises(SearchError):
            await p.search("x")
    finally:
        await p.close()


def test_search_candidate_defaults():
    c = SearchCandidate(title="t", share_url="https://pan.quark.cn/s/f32d0e563a19")
    assert c.passcode == "" and c.score == 0.0


# ---------- 多网盘类型搜索（独立搜索模式） ----------

MULTIDISK_BODY = {
    "code": 0, "message": "success",
    "data": {
        "total": 3,
        "merged_by_type": {
            "quark": [{"url": "https://pan.quark.cn/s/f32d0e563a19",
                       "note": "夸克资源", "datetime": "", "source": "p1"}],
            "uc": [{"url": "https://drive.uc.cn/s/abcdef123456",
                    "note": "UC资源", "datetime": "", "source": "p2"}],
            "baidu": [{"url": "https://pan.baidu.com/s/1abc-XYZ?",
                       "password": "ax72", "note": "百度资源",
                       "datetime": "", "source": "p3"},
                      {"url": "ftp://not-http.example/file",
                       "note": "非 http 脏数据", "datetime": "", "source": "p4"}],
        },
    },
}


async def test_search_all_disks_labels_and_loose_urls():
    """cloud_types=[] → 不限类型；每条候选带 disk 标签；非夸克宽松校验。"""
    def handler(request: Request) -> Response:
        body = request.read()
        assert b"cloud_types" not in body  # 全部网盘：请求体不带类型过滤
        return Response(200, json=MULTIDISK_BODY)

    p = make_provider(handler)
    try:
        cands = await p.search("余罪", cloud_types=[])
    finally:
        await p.close()
    assert [(c.disk, c.title) for c in cands] == [
        ("quark", "夸克资源"), ("uc", "UC资源"), ("baidu", "百度资源")]
    assert cands[2].passcode == "ax72"


async def test_search_selected_disks_filters_types():
    """cloud_types=["uc"] → 仅返回对应 merged_by_type 分组。"""
    def handler(request: Request) -> Response:
        body = request.read()
        assert b'"cloud_types":["uc"]' in body
        return Response(200, json=MULTIDISK_BODY)

    p = make_provider(handler)
    try:
        cands = await p.search("余罪", cloud_types=["uc"])
    finally:
        await p.close()
    assert [(c.disk, c.share_url) for c in cands] == [
        ("uc", "https://drive.uc.cn/s/abcdef123456")]


# ---------- 链接存活检测（/api/check/links） ----------

GOOD = "https://pan.quark.cn/s/f32d0e563a19"

CHECK_BODY = {"results": [
    {"disk_type": "quark", "url": "https://pan.quark.cn/s/f32d0e563a19",
     "normalized_url": "https://pan.quark.cn/s/f32d0e563a19", "state": "ok",
     "cache_hit": False, "checked_at": 1, "expires_at": 2, "summary": "链接有效"},
    {"disk_type": "quark", "url": "https://pan.quark.cn/s/2a5efb0c3dc7",
     "normalized_url": "https://pan.quark.cn/s/2a5efb0c3dc7", "state": "bad",
     "cache_hit": True, "checked_at": 3, "expires_at": 4, "summary": "分享不存在"},
]}


async def test_check_links_parses_states():
    def handler(request: Request) -> Response:
        assert request.url.path == "/api/check/links"
        assert request.method == "POST"
        body = request.read()
        assert b'"disk_type":"quark"' in body
        assert b'"password":"ax72"' in body
        return Response(200, json=CHECK_BODY)

    p = make_provider(handler)
    try:
        res = await p.check_links([
            {"disk_type": "quark", "url": "https://pan.quark.cn/s/f32d0e563a19"},
            {"disk_type": "quark", "url": "https://pan.quark.cn/s/2a5efb0c3dc7",
             "password": "ax72"},
        ])
    finally:
        await p.close()
    assert [r["state"] for r in res] == ["ok", "bad"]
    assert res[0]["summary"] == "链接有效"


async def test_check_links_transient_then_success():
    calls = {"n": 0}

    def handler(request: Request) -> Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(502, text="bad gateway")
        return Response(200, json=CHECK_BODY)

    p = make_provider(handler)
    try:
        res = await p.check_links([{"disk_type": "quark", "url": GOOD}])
        assert res[0]["state"] == "ok" and calls["n"] == 2
    finally:
        await p.close()


async def test_check_links_http_error_raises():
    def handler(request: Request) -> Response:
        return Response(500, text="oops")

    p = make_provider(handler)
    try:
        with pytest.raises(SearchError):
            await p.check_links([{"disk_type": "quark", "url": GOOD}])
    finally:
        await p.close()


async def test_check_links_missing_results_raises():
    def handler(request: Request) -> Response:
        return Response(200, json={"code": 400, "message": "items不能为空"})

    p = make_provider(handler)
    try:
        with pytest.raises(SearchError):
            await p.check_links([{"disk_type": "quark", "url": GOOD}])
    finally:
        await p.close()


# ---------- 瞬时失败重试与空结果补查 ----------

async def test_transient_error_then_success():
    """公开实例瞬时 400/502：重试后成功。"""
    calls = {"n": 0}

    def handler(request: Request) -> Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(502, text="bad gateway")
        return Response(200, json=PANSOU_BODY)

    p = make_provider(handler)
    try:
        cands = await p.search("余罪")
        assert len(cands) == 2 and calls["n"] == 2
    finally:
        await p.close()


async def test_empty_first_query_refills_from_cache():
    """插件异步搜索：首查 0 条 → 补查命中缓存。"""
    calls = {"n": 0}
    empty = {"code": 0, "message": "success", "data": {"total": 0, "merged_by_type": {}}}

    def handler(request: Request) -> Response:
        calls["n"] += 1
        return Response(200, json=PANSOU_BODY if calls["n"] > 1 else empty)

    p = make_provider(handler)
    try:
        cands = await p.search("余罪")
        assert len(cands) == 2 and calls["n"] == 2
    finally:
        await p.close()


async def test_persistent_empty_returns_empty():
    """连续两次空结果：如实返回空列表而非报错。"""
    empty = {"code": 0, "message": "success", "data": {"total": 0, "merged_by_type": {}}}

    def handler(request: Request) -> Response:
        return Response(200, json=empty)

    p = make_provider(handler)
    try:
        assert await p.search("x") == []
    finally:
        await p.close()
