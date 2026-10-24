"""Stream 业务编排：IMDb ID → TMDB 元数据 → 网盘搜索 → 匹配 → Stremio Stream。"""
import json
import logging

from . import config, db
from .tmdb import VideoMeta, fetch_meta
from .matcher import MovieMatcher, parse
from .providers.base import CloudDriveProvider, CloudFile, ProviderAuthError, ProviderError
from .providers.quark import QuarkDriveProvider

log = logging.getLogger(__name__)

_STATUS_TEXT = {
    "PENDING": "等待执行",
    "SUBMITTING": "正在提交转存",
    "PROCESSING": "转存处理中",
    "COMPLETED": "转存完成",
    "FAILED": "转存失败",
    "EXPIRED": "任务超时或失效",
}


def parse_video_id(video_id: str) -> tuple[str, int | None, int | None]:
    """"tt1254207" 或 "tt1254207:1:2" → (imdb_id, season, episode)。"""
    parts = video_id.split(":")
    imdb_id = parts[0]
    season = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    episode = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    return imdb_id, season, episode


class StreamService:
    def __init__(self, db_path: str = config.DB_PATH, threshold: float = config.MATCH_THRESHOLD):
        self.db_path = db_path
        self.matcher = MovieMatcher(threshold=threshold)

    # ---- Provider 管理 ----

    def load_cookie(self) -> str | None:
        return db.load_cookie(self.db_path) or (config.QUARK_COOKIE or None)

    def create_provider(self, cookie: str) -> CloudDriveProvider:
        return QuarkDriveProvider(cookie=cookie, db_path=self.db_path)

    # ---- 主流程 ----

    async def get_streams(self, vtype: str, video_id: str) -> tuple[list[dict], str]:
        """返回 (stremio stream 列表, 状态说明)。任何失败均返回空列表不抛出。"""
        cookie = self.load_cookie()
        if not cookie:
            return [], "未绑定网盘账号，请访问 /setup 完成授权"
        imdb_id, season, episode = parse_video_id(video_id)
        meta = await fetch_meta(imdb_id, vtype, self.db_path)
        if meta is None or not meta.title:
            return [], f"TMDB 未收录 {video_id}"
        provider = self.create_provider(cookie)
        try:
            return await self._streams_for(provider, meta, vtype, video_id, season, episode)
        except ProviderAuthError as e:
            log.error("网盘登录态失效: %s", e)
            return [], "网盘登录态失效，请重新在 /setup 授权"
        except ProviderError as e:
            log.error("网盘接口错误: %s", e)
            return [], f"网盘接口错误: {e}"
        except Exception:
            log.exception("获取播放流时发生未预期错误")
            return [], "内部错误"
        finally:
            await provider.close()

    async def _streams_for(self, provider: CloudDriveProvider, meta: VideoMeta,
                           vtype: str, video_id: str,
                           season: int | None, episode: int | None) -> tuple[list[dict], str]:
        imdb_id, _, _ = parse_video_id(video_id)
        files = await self._search_all(provider, meta, video_id)
        if not files:
            reason = f"网盘未搜索到“{meta.title}”"
            return self._with_guidance(vtype, video_id, reason), reason
        best = self.matcher.best(files, meta, season, episode)
        if best is None:
            # 转存任务已精准定位的文件可直接信任（Worker 选择时有分享结构上下文，
            # 能覆盖裸集数文件名 06.mkv 这类标题匹配必失败的场景）
            located = self._task_located_file(files, vtype, imdb_id, season, episode)
            if located is not None:
                resource = await provider.get_play_resource(located)
                return [self._to_stream(resource, "转存任务已定位该文件")], \
                    "转存任务已定位该文件"
            cands = self.matcher.match(files, meta, season, episode)
            detail = "；".join(f"{c.file.name}({c.score})" for c in cands[:3]) or "无候选"
            reason = f"置信度不足，暂不返回流（候选: {detail}）"
            return self._with_guidance(vtype, video_id, reason), reason
        resource = await provider.get_play_resource(best.file)
        return [self._to_stream(resource, best.reason)], best.reason

    def _task_located_file(self, files: list[CloudFile], vtype: str, imdb_id: str,
                           season: int | None, episode: int | None) -> CloudFile | None:
        """在搜索结果里找转存任务明确指定的目标文件（target_fid 或 result_json.name）。"""
        by_fid = {f.fid: f for f in files}
        for t in db.get_tasks_for_video(self.db_path, vtype, imdb_id,
                                        season, episode):
            if t["status"] != "COMPLETED":
                continue
            if t["target_fid"] and t["target_fid"] in by_fid:
                return by_fid[t["target_fid"]]
            try:
                name = (json.loads(t["result_json"]) or {}).get("name") or ""
            except ValueError:
                name = ""
            if name:
                for f in files:
                    if f.name == name:
                        return f
        return None

    async def _search_all(self, provider: CloudDriveProvider, meta: VideoMeta,
                          video_id: str) -> list[CloudFile]:
        """多关键词搜索网盘并按 fid 去重：TMDB 双语名（跨语言）
        + 已完成转存任务的定位文件名（转存文件名常与标题语言不一致）。"""
        imdb_id, season, episode = parse_video_id(video_id)
        # TMDB 双语名跨语言检索：中文名与外语名都参与（网盘文件命名语言不定）
        keywords = [meta.title, meta.name_zh, meta.name_en]
        for t in db.get_tasks_for_video(self.db_path, vtype=meta.type,
                                        imdb_id=imdb_id, season=season,
                                        episode=episode):
            if t["status"] != "COMPLETED" or not t["result_json"]:
                continue
            try:
                name = json.loads(t["result_json"]).get("name") or ""
            except ValueError:
                name = ""
            if name:
                keywords.append(name)
        seen: dict[str, CloudFile] = {}
        for kw in dict.fromkeys(k for k in keywords if k):
            try:
                for f in await provider.search_files(kw):
                    seen.setdefault(f.fid, f)
            except ProviderError as e:
                log.warning("网盘搜索“%s”失败: %s", kw, e)
        return list(seen.values())

    def _with_guidance(self, vtype: str, video_id: str, reason: str) -> list[dict]:
        """未命中时附加 externalUrl 引导流（Stremio 标准字段，点击在浏览器打开），
        不伪造播放流。已有转存任务时指向任务页并展示实时进度（阶段 4 播放闭环：
        转存完成后用户在 Stremio 重新刷新片源即可命中播放）。"""
        if not config.BASE_URL:
            return []
        url = f"{config.BASE_URL}/candidates?vtype={vtype}&id={video_id}"
        extra = "。点击打开候选资源页，选择资源后转存到网盘。"
        imdb_id, season, episode = parse_video_id(video_id)
        tasks = db.get_tasks_for_video(self.db_path, vtype, imdb_id, season, episode)
        if tasks:
            t = tasks[0]
            url = f"{config.BASE_URL}/task/{t['id']}"
            st = _STATUS_TEXT.get(t["status"], t["status"])
            if t["status"] in ("PENDING", "SUBMITTING", "PROCESSING"):
                extra = f"。该影片已有转存任务（{st}），完成后在 Stremio 重新刷新即可播放。"
            elif t["status"] == "COMPLETED":
                extra = "。转存已完成但文件名未能匹配播放，点击查看任务详情。"
            else:
                extra = f"。此前转存任务{st}，点击重新选择资源。"
        return [{
            "name": f"{config.ADDON_NAME}\n外部搜索",
            "title": "未在网盘找到匹配，点击查看候选资源或转存进度",
            "description": f"{reason}{extra}",
            "externalUrl": url,
        }]

    def _to_stream(self, resource, reason: str) -> dict:
        quality = parse(resource.filename).resolution or ""
        use_proxy = config.PROXY_STREAM and bool(config.BASE_URL)
        if use_proxy:
            from .proxy import make_proxy_url

            url = make_proxy_url(resource.fid)
        else:
            url = resource.url
        hints = {
            "notWebReady": not use_proxy,  # 代理后无需请求头，Web 端亦可直连
            "bingeGroup": f"{resource.source_name}-{parse(resource.filename).title or 'video'}",
            "filename": resource.filename,
            "videoSize": resource.video_size,
        }
        if not use_proxy:
            hints["proxyHeaders"] = {"request": resource.headers}
        return {
            "url": url,
            "name": f"{resource.source_name}\n{quality}".rstrip(),
            "title": resource.filename,
            "description": f"{reason}\n{resource.filename}",
            "behaviorHints": hints,
        }
