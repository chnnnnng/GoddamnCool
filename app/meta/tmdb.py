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
            for line in r.text.splitlines():
                parts = line.split()
                if len(parts) < 2 or config.TMDB_API_HOST not in parts[1:]:
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

    # ---------- 公共：元数据查询 ----------

    async def fetch_meta(self, imdb_id: str, vtype: str,
                         db_path: str | None = None,
                         transport: httpx.AsyncTransport | None = None) -> VideoMeta | None:
        t0 = time.monotonic()
        path = db_path or config.DB_PATH
        cached = db.get_tmdb_meta(path, imdb_id, vtype, ttl=config.TMDB_META_TTL)
        if cached:
            db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}.cache",
                               duration_ms=0, success=True,
                               meta={"imdb_id": imdb_id, "vtype": vtype})
            return VideoMeta(cached)
        key = settings.get("TMDB_API_KEY")
        if not key:
            db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}",
                               duration_ms=0, success=False,
                               error="TMDB_API_KEY 未配置",
                               meta={"imdb_id": imdb_id, "vtype": vtype})
            log.error("TMDB_API_KEY 未配置（请管理员在 /admin 配置页填写，或设置环境变量）")
            return None
        meta_rec = {"imdb_id": imdb_id, "vtype": vtype}
        try:
            async with httpx.AsyncClient(timeout=10, transport=transport,
                                         params={"api_key": key}) as client:
                zh, en = await asyncio.gather(
                    self._find(client, imdb_id, _LANG_ZH),
                    self._find(client, imdb_id, _LANG_EN),
                )
                key_map = "movie_results" if vtype == "movie" else "tv_results"
                entry = (zh.get(key_map) or [None])[0]
                if not entry:
                    db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}",
                                       duration_ms=int((time.monotonic() - t0) * 1000),
                                       success=False, error="TMDB 未收录",
                                       meta=meta_rec)
                    log.info("TMDB 未收录 %s (%s)", imdb_id, vtype)
                    return None
                peer = (en.get(key_map) or [{}])[0]
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
            db.save_tmdb_meta(path, imdb_id, vtype, data)
            dur_ms = int((time.monotonic() - t0) * 1000)
            db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}",
                               duration_ms=dur_ms, success=True,
                               meta={**meta_rec, "response": {
                                   "name_zh": name_zh, "name_en": name_en,
                                   "year": date[:4], "videos": len(videos)}})
            return VideoMeta(data)
        except Exception as e:
            dur_ms = int((time.monotonic() - t0) * 1000)
            db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}",
                               duration_ms=dur_ms, success=False,
                               error=str(e)[:200], meta=meta_rec)
            log.warning("TMDB 查询失败: %s %s", vtype, imdb_id, exc_info=True)
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
                if not imdb_id and vtype != "movie":
                    r2 = await self._api_get(client, f"/tv/{tmdb_id}/external_ids")
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
