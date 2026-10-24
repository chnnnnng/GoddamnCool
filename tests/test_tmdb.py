"""TMDB 客户端测试：find 双语言解析 / 剧集季集 / 缓存 / IP 直连降级（MockTransport）。"""
import ipaddress
import time

import httpx
import pytest
from cryptography.fernet import Fernet

from app import config, db
import app.tmdb as tmdb_mod

FIND_MOVIE_ZH = {"movie_results": [{"id": 24428, "title": "复仇者联盟",
                                    "release_date": "2012-04-25"}], "tv_results": []}
FIND_MOVIE_EN = {"movie_results": [{"id": 24428, "title": "The Avengers",
                                    "release_date": "2012-04-25"}], "tv_results": []}
FIND_TV_ZH = {"movie_results": [],
              "tv_results": [{"id": 60059, "name": "余罪", "first_air_date": "2016-05-09"}]}
FIND_TV_EN = {"movie_results": [],
              "tv_results": [{"id": 60059, "name": "Yu Zui", "first_air_date": "2016-05-09"}]}
TV_DETAIL = {"seasons": [{"season_number": 0, "name": "特别篇"},
                         {"season_number": 1, "name": "第一季"},
                         {"season_number": 2, "name": "第二季"}]}
SEASON = {"episodes": [{"episode_number": 1, "name": "初出茅庐"},
                       {"episode_number": 2, "name": "一二三"}]}

_HOSTS_TEXT = ("# CheckTMDB 每日更新\n"
               "18.155.173.7 api.themoviedb.org image.tmdb.org\n"
               "1.2.3.4 image.tmdb.org\n")


def make_transport(calls, hosts=None, block_domain=False, dead_ips=(),
                   hosts_text=_HOSTS_TEXT,
                   domain_exc=httpx.ConnectError("DNS 污染")):
    """模拟 TMDB 请求：按 path 路由；可模拟域名污染与 IP 失效场景。

    hosts 列表记录各请求的目标主机；对 IP 直连请求断言 SNI 与 Host 头
    仍指向 api.themoviedb.org（等价于 hosts 解析的效果）。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if hosts is not None:
            hosts.append(request.url.host)
        if request.url.host == "cdn.jsdelivr.net":  # CheckTMDB hosts 表
            return httpx.Response(200, text=hosts_text)
        if request.url.host != config.TMDB_API_HOST:  # IP 直连请求
            assert request.headers["host"] == config.TMDB_API_HOST
            assert request.extensions.get("sni_hostname") == config.TMDB_API_HOST
            ipaddress.ip_address(request.url.host)
        elif block_domain:
            raise domain_exc
        if request.url.host in dead_ips:
            raise httpx.ConnectError("IP 失效")
        path, lang = request.url.path, request.url.params.get("language", "")
        if path.startswith("/3/find/"):
            zh, en = ((FIND_MOVIE_ZH, FIND_MOVIE_EN) if "tt0848228" in path
                      else (FIND_TV_ZH, FIND_TV_EN))
            return httpx.Response(200, json=zh if lang == "zh-CN" else en)
        if path == "/3/tv/60059":
            return httpx.Response(200, json=TV_DETAIL)
        if path.startswith("/3/tv/60059/season/"):
            return httpx.Response(200, json=SEASON)
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def meta_env(monkeypatch, tmp_path):
    """隔离：临时库 + 假 key + 复位访问主机缓存。"""
    path = str(tmp_path / "meta.db")
    db.init(path, Fernet.generate_key().decode())
    monkeypatch.setattr(config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(tmdb_mod, "_host", "")
    monkeypatch.setattr(tmdb_mod, "_host_ts", 0.0)
    return path


# ---------- 元数据解析 ----------

async def test_fetch_movie_bilingual(meta_env):
    calls = []
    m = await tmdb_mod.fetch_meta("tt0848228", "movie", meta_env,
                                  transport=make_transport(calls))
    assert m.name_zh == "复仇者联盟" and m.name_en == "The Avengers"
    assert m.title == "复仇者联盟" and m.year == 2012 and m.videos == []
    assert calls.count("/3/find/tt0848228") == 2  # zh-CN / en-US 各一次


async def test_fetch_tv_episodes(meta_env):
    m = await tmdb_mod.fetch_meta("tt5925192", "series", meta_env,
                                  transport=make_transport([]))
    assert m.name_zh == "余罪" and m.name_en == "Yu Zui"
    assert [v["episode"] for v in m.videos if v["season"] == 1] == [1, 2]
    assert m.episode_title(1, 2) == "一二三"
    assert all(v["season"] != 0 for v in m.videos)  # 特别篇（season 0）跳过


# ---------- 缓存 ----------

async def test_cache_hit_then_expired(meta_env, monkeypatch):
    calls = []
    m1 = await tmdb_mod.fetch_meta("tt0848228", "movie", meta_env,
                                   transport=make_transport(calls))
    n = len(calls)
    m2 = await tmdb_mod.fetch_meta("tt0848228", "movie", meta_env,
                                   transport=make_transport(calls))
    assert len(calls) == n  # 命中缓存：TMDB 零请求
    assert m2.title == m1.title
    monkeypatch.setattr(config, "TMDB_META_TTL", 0.0)  # TTL 过期 → 重新查询
    await tmdb_mod.fetch_meta("tt0848228", "movie", meta_env,
                              transport=make_transport(calls))
    assert len(calls) > n


# ---------- 降级 ----------

async def test_not_found_returns_none(meta_env):
    def handler(request):
        return httpx.Response(200, json={"movie_results": [], "tv_results": []})

    m = await tmdb_mod.fetch_meta("tt9999999", "movie", meta_env,
                                  transport=httpx.MockTransport(handler))
    assert m is None


async def test_network_failure_returns_none(meta_env):
    def handler(request):
        raise httpx.ConnectError("boom")

    m = await tmdb_mod.fetch_meta("tt0848228", "movie", meta_env,
                                  transport=httpx.MockTransport(handler))
    assert m is None


async def test_missing_key_returns_none(meta_env, monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "")
    calls = []
    m = await tmdb_mod.fetch_meta("tt0848228", "movie", meta_env,
                                  transport=make_transport(calls))
    assert m is None and calls == []  # 不发起任何请求


# ---------- CheckTMDB IP 直连（境内 DNS 污染场景） ----------

async def test_domain_direct_no_checktmdb(meta_env):
    calls, hosts = [], []
    m = await tmdb_mod.fetch_meta("tt0848228", "movie", meta_env,
                                  transport=make_transport(calls, hosts))
    assert m.title == "复仇者联盟"
    assert "cdn.jsdelivr.net" not in hosts  # 域名可达时不请求 CheckTMDB


async def test_domain_failure_switches_to_ip(meta_env):
    calls, hosts = [], []
    m = await tmdb_mod.fetch_meta("tt5925192", "series", meta_env,
                                  transport=make_transport(calls, hosts,
                                                           block_domain=True))
    assert m.name_zh == "余罪" and m.name_en == "Yu Zui"
    assert hosts.count("cdn.jsdelivr.net") >= 1   # 拉取了 CheckTMDB IP 表
    assert hosts.count("18.155.173.7") >= 1       # 后续请求走 IP 直连
    assert tmdb_mod._host == "18.155.173.7"       # 进程内缓存当前 IP
    assert "/3/tv/60059/season/1" in calls        # 季集请求也走 IP 直连


async def test_domain_timeout_also_switches_to_ip(meta_env):
    """DNS 污染常表现为连接超时（ConnectTimeout），同样触发 IP 直连切换。"""
    calls, hosts = [], []
    m = await tmdb_mod.fetch_meta(
        "tt0848228", "movie", meta_env,
        transport=make_transport(calls, hosts, block_domain=True,
                                 domain_exc=httpx.ConnectTimeout("超时")))
    assert m.title == "复仇者联盟"
    assert hosts.count("18.155.173.7") >= 1


async def test_ip_transient_timeout_retried(meta_env, monkeypatch):
    """IP 直连偶发瞬时超时：同一 IP 自动重试一次，不误判为 IP 失效。"""
    monkeypatch.setattr(tmdb_mod, "_host", "18.155.173.7")
    monkeypatch.setattr(tmdb_mod, "_host_ts", time.time())
    calls, hosts = [], []
    state = {"failed": False}

    def handler(request):
        if request.url.host == "18.155.173.7" and not state["failed"]:
            state["failed"] = True  # 仅首次 IP 请求注入超时
            raise httpx.ConnectTimeout("瞬时超时")
        return make_transport(calls, hosts).handler(request)

    m = await tmdb_mod.fetch_meta("tt0848228", "movie", meta_env,
                                  transport=httpx.MockTransport(handler))
    assert m.title == "复仇者联盟"            # 重试后成功
    assert hosts.count("18.155.173.7") >= 2   # 同一 IP 重试


async def test_stale_ip_refreshed(meta_env, monkeypatch):
    """缓存的 IP 连不上时：重新拉取 CheckTMDB 最新 IP 并换新重试。"""
    monkeypatch.setattr(tmdb_mod, "_host", "9.9.9.9")
    monkeypatch.setattr(tmdb_mod, "_host_ts", time.time())
    calls, hosts = [], []
    m = await tmdb_mod.fetch_meta("tt0848228", "movie", meta_env,
                                  transport=make_transport(calls, hosts,
                                                           dead_ips=("9.9.9.9",)))
    assert m.title == "复仇者联盟"
    assert "9.9.9.9" in hosts                      # 旧 IP 先失败
    assert tmdb_mod._host == "18.155.173.7"        # 已换新 IP


async def test_fetch_ip_parses_hosts(meta_env):
    text = ("# comment line\n"
            "not-an-ip api.themoviedb.org\n"
            "18.155.173.7 api.themoviedb.org image.tmdb.org\n"
            "1.2.3.4 image.tmdb.org\n")

    def handler(request):
        return httpx.Response(200, text=text)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await tmdb_mod._fetch_ip(c) == "18.155.173.7"

    def empty(request):
        return httpx.Response(200, text="# 只有注释，无条目")

    async with httpx.AsyncClient(transport=httpx.MockTransport(empty)) as c:
        assert await tmdb_mod._fetch_ip(c) == ""   # 无条目返回空串
