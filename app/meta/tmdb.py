"""TMDB 元数据源：CheckTMDB IP 直连 + SQLite 缓存。

原 app/tmdb.py 里的顶层函数全部迁入 TMDBMetaProvider 作为实例方法；
VideoMeta 统一在 meta/base.py 定义。"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time

import httpx

from .. import config, db, settings
from .base import MetaProvider, VideoMeta

log = logging.getLogger(__name__)

_LANG_ZH, _LANG_EN = "zh-CN", "en-US"
_HOST_REFRESH = 86400.0


class TMDBMetaProvider(MetaProvider):
    """TMDB 元数据源。境内直连受 DNS 污染影响，失败时自动用 CheckTMDB 提供的 IP。"""

    def __init__(self):
        self._host = ""       # 当前 TMDB 访问主机（域名或 IP），进程内缓存
        self._host_ts = 0.0

    @property
    def configured(self) -> bool:
        key = settings.get("TMDB_API_KEY") or ""
        return bool(key.strip())

    # ---------- 内部：可达性 ----------

    async def _api_get(self, client: httpx.AsyncClient, path: str,
                       params: dict | None = None) -> httpx.Response:
        global _HOST_REFRESH  # noqa: F841  (keep style consistent)
        if time.time() - self._host_ts >= _HOST_REFRESH:
            self._host, self._host_ts = config.TMDB_API_HOST, time.time()
        try:
            return await self._get(client, self._host, path, params)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            ip = await self._fetch_ip(client)
            if ip and ip != self._host:
                log.info("TMDB 连接失败，改用 CheckTMDB IP 直连: %s", ip)
                self._host, self._host_ts = ip, time.time()
            elif self._host == config.TMDB_API_HOST:
                raise
        last_err: Exception = RuntimeError("unreachable")
        for _ in range(2):
            try:
                return await self._get(client, self._host, path, params)
            except (httpx.ConnectError, httpx.ConnectTimeout) as err:
                last_err = err
        raise last_err

    async def _get(self, client, host, path, params):
        base = config.TMDB_BASE if host == config.TMDB_API_HOST else f"https://{host}/3"
        kwargs = {"params": params}
        if host != config.TMDB_API_HOST:
            kwargs.update(headers={"Host": config.TMDB_API_HOST},
                          extensions={"sni_hostname": config.TMDB_API_HOST})
        return await client.get(f"{base}{path}", **kwargs)

    async def _fetch_ip(self, client):
        try:
            r = await client.get(config.CHECKTMDB_HOSTS_URL)
            r.raise_for_status()
            # hosts 表里 api.tmdb.org 和 api.themoviedb.org 都有，
            # 两个域名都要匹配（默认 host=api.tmdb.org，回退时可能用 api.themoviedb.org）
            known_hosts = {"api.tmdb.org", "api.themoviedb.org", config.TMDB_API_HOST}
            for line in r.text.splitlines():
                parts = line.split()
                if len(parts) < 2 or not any(h in parts[1:] for h in known_hosts):
                    continue
                try:
                    ipaddress.ip_address(parts[0])
                except ValueError:
                    continue
                return parts[0]
            log.warning("CheckTMDB hosts 中没有 %s 条目", config.TMDB_API_HOST)
        except Exception:
            log.warning("CheckTMDB hosts 获取失败（%s 将不可达）", config.TMDB_API_HOST,
                        exc_info=True)
        return ""

    # ---------- 内部：TMDB 端点 ----------

    async def _find(self, client, imdb_id, language):
        r = await self._api_get(client, f"/find/{imdb_id}",
                                {"external_source": "imdb_id", "language": language})
        r.raise_for_status()
        return r.json()

    async def _tv_episodes(self, client, tmdb_id):
        r = await self._api_get(client, f"/tv/{tmdb_id}", {"language": _LANG_ZH})
        r.raise_for_status()
        seasons = [s["season_number"] for s in r.json().get("seasons", [])
                   if s.get("season_number")]
        if not seasons:
            return []

        async def one(sn):
            rs = await self._api_get(client, f"/tv/{tmdb_id}/season/{sn}",
                                     {"language": _LANG_ZH})
            rs.raise_for_status()
            return [{"season": sn, "episode": e["episode_number"],
                     "name": e.get("name") or ""}
                    for e in rs.json().get("episodes", []) if e.get("episode_number")]

        per_season = await asyncio.gather(*(one(s) for s in seasons))
        return [v for eps in per_season for v in eps]

    async def _external_ids(self, client, vtype: str, tmdb_id) -> dict:
        """TMDB search/find 结果不带 imdb_id，需要补调 external_ids。"""
        kind_path = "movie" if vtype == "movie" else "tv"
        try:
            r = await self._api_get(client, f"/{kind_path}/{tmdb_id}/external_ids")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("补调 external_ids 失败 %s/%s: %s", kind_path, tmdb_id, e)
            return {}

    # ---------- 公共：元数据查询 ----------

    async def fetch_meta(self, source_id: str, vtype: str,
                         db_path: str | None = None,
                         source_kind: str = "imdb",
                         transport: httpx.AsyncTransport | None = None) -> VideoMeta | None:
        """按外部 ID 拉元数据。

        source_kind 决定查询路径：
          - "imdb":    /find/{imdb_id}?external_source=imdb_id（Stremio 默认传）
          - "tmdb":    /movie|tv/{tmdb_id} + external_ids 补拿 imdb_id
          - "keyword": 委托给 fetch_meta_by_keyword
        """
        # keyword 直接委托（已经实现好）
        if source_kind == "keyword":
            meta, _ = await self.fetch_meta_by_keyword(source_id, vtype, db_path)
            return meta

        t0 = time.monotonic()
        path = db_path or config.DB_PATH
        # 缓存 key 加 source_kind 前缀，避免 imdb_id "tt123" 和 tmdb_id "123" 撞车
        cache_id = f"{source_kind}:{source_id}"
        cached = db.get_tmdb_meta(path, cache_id, vtype, ttl=config.TMDB_META_TTL)
        if cached:
            db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}.cache",
                               duration_ms=0, success=True,
                               meta={"source_id": source_id, "source_kind": source_kind,
                                     "vtype": vtype})
            return VideoMeta(cached, source_id=source_id, source_kind=source_kind)

        key = settings.get("TMDB_API_KEY")
        if not key:
            db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}",
                               duration_ms=0, success=False,
                               error="TMDB_API_KEY 未配置",
                               meta={"source_id": source_id, "source_kind": source_kind,
                                     "vtype": vtype})
            log.error("TMDB_API_KEY 未配置（请管理员在 /admin 配置页填写，或设置环境变量）")
            return None

        meta_rec = {"source_id": source_id, "source_kind": source_kind, "vtype": vtype}
        try:
            async with httpx.AsyncClient(timeout=10, transport=transport,
                                         params={"api_key": key}) as client:
                if source_kind == "imdb":
                    zh, en = await asyncio.gather(
                        self._find(client, source_id, _LANG_ZH),
                        self._find(client, source_id, _LANG_EN),
                    )
                    key_map = "movie_results" if vtype == "movie" else "tv_results"
                    entry = (zh.get(key_map) or [None])[0]
                    if not entry:
                        db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}",
                                           duration_ms=int((time.monotonic() - t0) * 1000),
                                           success=False, error="TMDB 未收录",
                                           meta=meta_rec)
                        log.info("TMDB 未收录 %s (%s:%s)", source_id, source_kind, vtype)
                        return None
                    peer = (en.get(key_map) or [{}])[0]
                    # external_ids：/find 不返回 imdb_id，必须补调
                    tmdb_id = entry["id"]
                    ext = await self._external_ids(client, vtype, tmdb_id)
                    imdb_id = ext.get("imdb_id") or ""

                elif source_kind == "tmdb":
                    kind_path = "movie" if vtype == "movie" else "tv"
                    zh_resp, en_resp = await asyncio.gather(
                        client.get(f"/{kind_path}/{source_id}", params={"language": _LANG_ZH}),
                        client.get(f"/{kind_path}/{source_id}", params={"language": _LANG_EN}),
                    )
                    zh_resp.raise_for_status()
                    en_resp.raise_for_status()
                    entry = zh_resp.json()
                    peer = en_resp.json()
                    tmdb_id = source_id
                    ext = await self._external_ids(client, vtype, tmdb_id)
                    imdb_id = ext.get("imdb_id") or ""
                else:
                    log.warning("未知 source_kind=%s，按 imdb 处理", source_kind)
                    return None

                name_zh = (entry.get("title") or entry.get("name") or "").strip()
                name_en = (peer.get("title") or peer.get("name") or "").strip()
                date = entry.get("release_date") or entry.get("first_air_date") or ""
                videos = (await self._tv_episodes(client, entry["id"])) if vtype != "movie" else []
                poster_path = entry.get("poster_path") or None
                backdrop_path = entry.get("backdrop_path") or None
                data = {"imdb_id": imdb_id, "type": vtype,
                        "name": name_zh or name_en,
                        "name_zh": name_zh, "name_en": name_en,
                        "releaseInfo": date[:4], "videos": videos,
                        "poster_path": poster_path, "backdrop_path": backdrop_path}
            db.save_tmdb_meta(path, cache_id, vtype, data)
            dur_ms = int((time.monotonic() - t0) * 1000)
            db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}",
                               duration_ms=dur_ms, success=True,
                               meta={**meta_rec, "response": {
                                   "imdb_id": imdb_id, "name_zh": name_zh, "name_en": name_en,
                                   "year": date[:4], "videos": len(videos)}})
            return VideoMeta(data, source_id=source_id, source_kind=source_kind)
        except Exception as e:
            dur_ms = int((time.monotonic() - t0) * 1000)
            db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}",
                               duration_ms=dur_ms, success=False,
                               error=str(e)[:200], meta=meta_rec)
            log.warning("TMDB 查询失败: %s:%s %s", source_kind, vtype, source_id, exc_info=True)
            return None

    async def fetch_meta_by_keyword(self, keyword: str, vtype: str,
                                    db_path: str | None = None) -> tuple[VideoMeta | None, str]:
        t0 = time.monotonic()
        path = db_path or config.DB_PATH
        key = settings.get("TMDB_API_KEY")
        if not key:
            return None, ""
        kw = keyword.strip()
        if not kw:
            return None, ""

        _CACHE_KEY = f"kw:{vtype}:{kw.lower()}"
        _KW_TTL = 3 * 86400
        cached = db.get_tmdb_meta(path, _CACHE_KEY, vtype, ttl=_KW_TTL)
        if cached:
            saved_imdb = cached.get("imdb_id") or ""
            if saved_imdb:
                video_meta = await self.fetch_meta(saved_imdb, vtype, path)
                if video_meta:
                    return video_meta, saved_imdb
            return None, ""

        try:
            async with httpx.AsyncClient(timeout=10, params={"api_key": key}) as client:
                search_path = "/search/movie" if vtype == "movie" else "/search/tv"
                r = await self._api_get(client, search_path, {
                    "query": kw,
                    "language": _LANG_ZH,
                    "include_adult": "false",
                    "page": 1,
                })
                r.raise_for_status()
                results = (r.json().get("results") or [])
                if not results:
                    db.save_tmdb_meta(path, _CACHE_KEY, vtype, {"imdb_id": ""})
                    return None, ""
                top = results[0]
                tmdb_id = top.get("id")
                if not tmdb_id:
                    return None, ""
                imdb_id = top.get("imdb_id") or ""
                # TMDB /search/movie 和 /search/tv 返回结果都不带 imdb_id，
                # 需要调 /{movie|tv}/{tmdb_id}/external_ids 补拿
                if not imdb_id:
                    kind = "movie" if vtype == "movie" else "tv"
                    r2 = await self._api_get(client, f"/{kind}/{tmdb_id}/external_ids")
                    r2.raise_for_status()
                    imdb_id = (r2.json().get("imdb_id") or "").strip()

                dur_ms = int((time.monotonic() - t0) * 1000)
                db.record_api_call(path, kind="tmdb", endpoint=f"search.{vtype}",
                                   duration_ms=dur_ms, success=True,
                                   meta={"keyword": kw, "tmdb_id": tmdb_id,
                                         "imdb_id": imdb_id})
                if not imdb_id:
                    db.save_tmdb_meta(path, _CACHE_KEY, vtype, {"imdb_id": ""})
                    return None, ""

                db.save_tmdb_meta(path, _CACHE_KEY, vtype, {"imdb_id": imdb_id})
                video_meta = await self.fetch_meta(imdb_id, vtype, path)
                return video_meta, imdb_id
        except Exception:
            log.warning("TMDB 关键词搜索失败: %s %s", vtype, kw, exc_info=True)
            db.record_api_call(path, kind="tmdb", endpoint=f"search.{vtype}",
                               duration_ms=int((time.monotonic() - t0) * 1000),
                               success=False, meta={"keyword": kw})
            return None, ""
