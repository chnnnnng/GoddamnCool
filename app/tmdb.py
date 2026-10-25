"""TMDB 客户端：IMDb ID → 双语标题/年份/季集（SQLite 缓存，TTL 7 天）。

境内直连 api.themoviedb.org 受 DNS 污染影响：域名连接失败时自动改用
CheckTMDB（github.com/cnwikee/CheckTMDB，每日更新 TMDB 可用 IP）提供的
IP 直连——请求 URL 直接指向 IP，经 httpx 的 sni_hostname 扩展把 TLS SNI、
Host 头与证书校验都指向原域名（等价于 hosts 解析，无需改 /etc/hosts）。
访问主机进程内缓存 24h；IP 连不上时自动重新拉取最新 IP 重试一次。
"""
import asyncio
import ipaddress
import logging
import time

import httpx

from . import config, db, settings

log = logging.getLogger(__name__)

_LANG_ZH, _LANG_EN = "zh-CN", "en-US"
_HOST_REFRESH = 86400.0
_host = ""     # 当前 TMDB API 访问主机（域名或 IP），进程内缓存
_host_ts = 0.0


class VideoMeta:
    """影片元数据（TMDB）：title 为主名（中文优先），name_zh/name_en 为双语名。"""

    def __init__(self, meta: dict):
        self.id: str = meta.get("imdb_id", "")
        self.name_zh: str = meta.get("name_zh", "") or ""
        self.name_en: str = meta.get("name_en", "") or ""
        self.title: str = meta.get("name", "") or self.name_zh or self.name_en
        self.type: str = meta.get("type", "")
        year_raw = meta.get("releaseInfo", "") or meta.get("year", "")
        self.year: int | None = _parse_year(year_raw)
        self.videos: list[dict] = meta.get("videos", []) or []

    def episode_title(self, season: int, episode: int) -> str | None:
        for v in self.videos:
            if v.get("season") == season and v.get("episode") == episode:
                return v.get("name") or v.get("title")
        return None


def _parse_year(raw) -> int | None:
    for token in str(raw).replace("–", "-").split("-"):
        token = token.strip()
        if token.isdigit() and 1900 <= int(token) <= 2100:
            return int(token)
    return None


# ---- TMDB 可达性：域名直连失败 → CheckTMDB IP 直连 ----

async def _api_get(client: httpx.AsyncClient, path: str,
                   params: dict | None = None) -> httpx.Response:
    """请求 TMDB API：域名直连失败（DNS 污染或 IP 失效）时改用 CheckTMDB
    提供的 IP 直连；CDN 边缘节点偶发瞬时超时，同一目标再重试一次。"""
    global _host, _host_ts
    if time.time() - _host_ts >= _HOST_REFRESH:
        _host, _host_ts = config.TMDB_API_HOST, time.time()
    try:
        return await _get(client, _host, path, params)
    except (httpx.ConnectError, httpx.ConnectTimeout):  # 污染表现为连接失败或超时
        ip = await _fetch_ip(client)
        if ip and ip != _host:
            log.info("TMDB 连接失败，改用 CheckTMDB IP 直连: %s", ip)
            _host, _host_ts = ip, time.time()
        elif _host == config.TMDB_API_HOST:
            raise  # 域名连不上且拿不到可用 IP，无法自救
    last_err: Exception = RuntimeError("unreachable")
    for _ in range(2):  # IP 直连；边缘节点偶发瞬时超时，重试一次
        try:
            return await _get(client, _host, path, params)
        except (httpx.ConnectError, httpx.ConnectTimeout) as err:
            last_err = err
    raise last_err


async def _get(client: httpx.AsyncClient, host: str, path: str,
               params: dict | None) -> httpx.Response:
    base = config.TMDB_BASE if host == config.TMDB_API_HOST else f"https://{host}/3"
    kwargs = {"params": params}
    if host != config.TMDB_API_HOST:
        # IP 直连：SNI、Host 头与证书校验仍指向原域名（等价于 hosts 解析的效果）
        kwargs.update(headers={"Host": config.TMDB_API_HOST},
                      extensions={"sni_hostname": config.TMDB_API_HOST})
    return await client.get(f"{base}{path}", **kwargs)


async def _fetch_ip(client: httpx.AsyncClient) -> str:
    """从 CheckTMDB hosts 表解析 api.themoviedb.org 的可用 IP（失败返回空串）。"""
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


# ---- 元数据查询 ----

async def fetch_meta(imdb_id: str, vtype: str, db_path: str | None = None,
                     transport: httpx.AsyncTransport | None = None) -> VideoMeta | None:
    """查询 TMDB（优先缓存）。未收录/网络失败返回 None（调用方降级处理）。

    API Key 由管理员在 /admin 配置页填写（settings），缺省回退服务端
    环境变量/根目录 TMDB_API_KEY 文件。"""
    import time as _time
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
    t0 = _time.monotonic()
    meta_rec = {"imdb_id": imdb_id, "vtype": vtype}
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport,
                                     params={"api_key": key}) as client:
            zh, en = await asyncio.gather(
                _find(client, imdb_id, _LANG_ZH),
                _find(client, imdb_id, _LANG_EN),
            )
            key = "movie_results" if vtype == "movie" else "tv_results"
            entry = (zh.get(key) or [None])[0]
            if not entry:
                db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}",
                                   duration_ms=int((_time.monotonic() - t0) * 1000),
                                   success=False,
                                   error="TMDB 未收录",
                                   meta=meta_rec)
                log.info("TMDB 未收录 %s (%s)", imdb_id, vtype)
                return None
            peer = (en.get(key) or [{}])[0]
            name_zh = (entry.get("title") or entry.get("name") or "").strip()
            name_en = (peer.get("title") or peer.get("name") or "").strip()
            date = entry.get("release_date") or entry.get("first_air_date") or ""
            videos = (await _tv_episodes(client, entry["id"])) if vtype != "movie" else []
            data = {"imdb_id": imdb_id, "type": vtype,
                    "name": name_zh or name_en,
                    "name_zh": name_zh, "name_en": name_en,
                    "releaseInfo": date[:4], "videos": videos}
        db.save_tmdb_meta(path, imdb_id, vtype, data)
        dur_ms = int((_time.monotonic() - t0) * 1000)
        db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}",
                           duration_ms=dur_ms, success=True, meta=meta_rec)
        return VideoMeta(data)
    except Exception as e:
        dur_ms = int((_time.monotonic() - t0) * 1000)
        db.record_api_call(path, kind="tmdb", endpoint=f"fetch_meta.{vtype}",
                           duration_ms=dur_ms, success=False,
                           error=str(e)[:200], meta=meta_rec)
        log.warning("TMDB 查询失败: %s %s", vtype, imdb_id, exc_info=True)
        return None


async def _find(client: httpx.AsyncClient, imdb_id: str, language: str) -> dict:
    r = await _api_get(client, f"/find/{imdb_id}",
                       {"external_source": "imdb_id", "language": language})
    r.raise_for_status()
    return r.json()


async def _tv_episodes(client: httpx.AsyncClient, tmdb_id) -> list[dict]:
    """拉取全部正片季集（并行逐季；特别篇 season 0 跳过）。"""
    r = await _api_get(client, f"/tv/{tmdb_id}", {"language": _LANG_ZH})
    r.raise_for_status()
    seasons = [s["season_number"] for s in r.json().get("seasons", [])
               if s.get("season_number")]
    if not seasons:
        return []

    async def one(sn: int) -> list[dict]:
        rs = await _api_get(client, f"/tv/{tmdb_id}/season/{sn}",
                            {"language": _LANG_ZH})
        rs.raise_for_status()
        return [{"season": sn, "episode": e["episode_number"],
                 "name": e.get("name") or ""}
                for e in rs.json().get("episodes", []) if e.get("episode_number")]

    per_season = await asyncio.gather(*(one(s) for s in seasons))
    return [v for eps in per_season for v in eps]
