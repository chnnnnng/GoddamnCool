"""Stream 业务编排：IMDb ID → MetaProvider 元数据 → 网盘搜索 → 匹配 → Stremio Stream。"""
import json
import logging

from . import config, db, settings
from .name_parser import parser as name_parser
from .meta import get_meta_provider
from .meta.base import VideoMeta
from .matcher import MovieMatcher, episode_of, parse
from .providers.base import CloudDriveProvider, CloudFile, ProviderAuthError, ProviderError
from .providers.quark import QuarkDriveProvider

# ---------- 向后兼容：模块级 fetch_meta 包装（测试 monkeypatch 依赖此符号） ----------
async def fetch_meta(source_id: str, vtype: str, db_path: str | None = None,
                     source_kind: str = "imdb") -> VideoMeta | None:
    return await get_meta_provider().fetch_meta(
        source_id, vtype, db_path, source_kind=source_kind)

log = logging.getLogger(__name__)

_STATUS_TEXT = {
    "PENDING": "等待执行",
    "SUBMITTING": "正在提交转存",
    "PROCESSING": "转存处理中",
    "COMPLETED": "转存完成",
    "FAILED": "转存失败",
    "EXPIRED": "任务超时或失效",
}


def _spec_emoji_line(info) -> str:
    """文件名规格 → Stremio 描述行（需求第 11 条）：
    🎬 分辨率/来源/编码 ✨ 附加（HDR/DV等） 🎧 音频 💬 字幕/语言。"""
    parts = []
    main = [v for v in (info.resolution, info.source, info.codec) if v]
    if main:
        parts.append("🎬 " + " · ".join(main))
    if info.extras:
        parts.append("✨ " + "/".join(info.extras))
    if info.audio:
        parts.append("🎧 " + "/".join(info.audio))
    sub = list(dict.fromkeys([*info.subtitle, *info.languages]))
    if sub:
        parts.append("💬 " + "/".join(sub))
    return " ".join(parts)


def parse_video_id(video_id: str) -> tuple[str, int | None, int | None]:
    """"tt1254207" 或 "tt1254207:1:2" → (imdb_id, season, episode)。"""
    parts = video_id.split(":")
    imdb_id = parts[0]
    season = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    episode = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    return imdb_id, season, episode


class StreamService:
    def __init__(self, db_path: str = config.DB_PATH, threshold: float | None = None):
        self.db_path = db_path
        # 匹配阈值支持管理员在线修改（settings），未显式传参时运行时读取
        self.matcher = MovieMatcher(
            threshold=settings.get("MATCH_THRESHOLD") if threshold is None
            else threshold)

    # ---- Provider 管理 ----

    def create_provider(self, cookie: str,
                        user_id: str = "") -> CloudDriveProvider:
        return QuarkDriveProvider(cookie=cookie, db_path=self.db_path,
                                  user_id=user_id)

    # ---- 主流程 ----

    async def get_streams(self, vtype: str, video_id: str, cookie: str,
                          user_id: str | None = None) -> tuple[list[dict], str]:
        """返回 (stremio stream 列表, 状态说明)。任何失败均返回列表不抛出。

        cookie: 调用者自己绑定的网盘 Cookie（多用户隔离，必传）；
        user_id: 任务归属过滤（None 不过滤兼容旧调用；'' 仅全局任务）。
        TMDB key 由管理员统一配置（settings）。

        """
        return await self._get_streams_impl(vtype, video_id, cookie, user_id)

    async def _get_streams_impl(self, vtype: str, video_id: str, cookie: str,
                                user_id: str | None = None) -> tuple[list[dict], str]:
        if not cookie:
            return [], "未绑定网盘 Cookie，请在控制面板绑定"
        imdb_id, season, episode = parse_video_id(video_id)
        meta = await fetch_meta(imdb_id, vtype, self.db_path)
        if meta is None or not meta.title:
            return [], f"TMDB 未收录 {video_id}"
        provider = self.create_provider(cookie, user_id=user_id or "")
        try:
            return await self._streams_for(provider, meta, vtype, video_id,
                                           season, episode, user_id)
        except ProviderAuthError as e:
            log.error("网盘登录态失效: %s", e)
            return [], "网盘登录态失效，请在控制面板重新绑定 Cookie"
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
                           season: int | None, episode: int | None,
                           user_id: str | None = None) -> tuple[list[dict], str]:
        imdb_id, _, _ = parse_video_id(video_id)
        uid = user_id or ""

        # —— 路径 1：file_index 快路径（O(1)，完全绕开标盘搜索）——
        idx = db.get_file_index(self.db_path, user_id=uid,
                                imdb_id=imdb_id, vtype=vtype,
                                season=season, episode=episode)
        if idx and idx.get("fid"):
            try:
                resource = await provider.get_play_resource(
                    CloudFile(fid=idx["fid"], name=idx.get("filename", ""),
                              size=0, is_dir=False))
                reason = "file_index 快路径命中"
                return [self._to_stream(resource, reason)], reason
            except ProviderError as e:
                log.warning("file_index 命中但 fid 取直链失败（可能 Cookie 变更）: %s", e)

        # —— 路径 2：transfer_task 直接 fid（完全不搜盘）——
        task_file = self._resolve_fid_from_tasks(vtype, imdb_id, season,
                                                 episode, uid)
        if task_file is not None:
            try:
                resource = await provider.get_play_resource(task_file)
            except ProviderError as e:
                log.warning("transfer_task 直接路径 fid 取直链失败（可能文件已删）: %s，回退搜索匹配", e)
            else:
                db.upsert_file_index(self.db_path, user_id=uid,
                                     imdb_id=imdb_id, vtype=vtype,
                                     season=season, episode=episode,
                                     fid=task_file.fid, filename=task_file.name,
                                     source="task")
                reason = "转存任务已定位该文件"
                return [self._to_stream(resource, reason)], reason

        # —— 路径 3：网盘搜索 + 标题匹配（原有逻辑）——
        files = await self._search_all(provider, meta, video_id, user_id)
        if not files:
            reason = f"网盘未搜索到“{meta.title}”"
            return self._with_guidance(vtype, video_id, reason, user_id), reason
        best = self.matcher.best(files, meta, season, episode)
        if best is not None:
            resource = await provider.get_play_resource(best.file)
            db.upsert_file_index(self.db_path, user_id=uid,
                                 imdb_id=imdb_id, vtype=vtype,
                                 season=season, episode=episode,
                                 fid=best.file.fid, filename=best.file.name,
                                 source="matcher")
            return [self._to_stream(resource, best.reason)], best.reason

        # 路径 3b：fallback —— 置信度不足或同分争议 → 引导流
        cands = self.matcher.match(files, meta, season, episode)
        detail = "；".join(f"{c.file.name}({c.score})" for c in cands[:3]) or "无候选"
        reason = f"置信度不足，暂不返回流（候选: {detail}）"
        return self._with_guidance(vtype, video_id, reason, user_id), reason

    def _resolve_fid_from_tasks(self, vtype: str, imdb_id: str,
                                season: int | None, episode: int | None,
                                user_id: str | None) -> CloudFile | None:
        """从 COMPLETED 转存任务中直接解析 fid，完全不依赖网盘搜索结果。

        三种可靠来源（按优先级）：
        1. target_fid 非空 —— episode/picks 模式或整包单文件落盘（从 result_json
           尽量补齐文件名）
        2. whole/picks 模式下 result_json.files 清单 —— 按季集号匹配文件（覆盖
           "06.mkv" 等裸集数文件名标题匹配必失败的场景）
        3. result_json.name —— 整包任务的顶层目录名/主文件名（movie 场景）

        构造的 CloudFile 仅填 fid + name（get_play_resource 只需 fid），
        彻底绕开 `_search_all` 搜索不到规范名的问题。"""
        tasks = db.get_tasks_for_video(self.db_path, vtype, imdb_id,
                                       season, episode, user_id=user_id)
        for t in tasks:
            if t["status"] != "COMPLETED":
                continue
            # 预解析 result_json（多处使用）
            try:
                res = json.loads(t["result_json"]) if t["result_json"] else {}
            except ValueError:
                res = {}

            # 1) target_fid（最可靠，Worker 已写死）
            if t["target_fid"]:
                name = ""
                # 从 result_json 尽量补齐文件名 —— 整包清单 / result_json.name
                for entry in res.get("files") or []:
                    if entry.get("fid") == t["target_fid"]:
                        name = entry.get("name") or ""
                        break
                if not name:
                    name = res.get("name") or ""
                return CloudFile(fid=t["target_fid"],
                                 name=name, size=0, is_dir=False)
            # 2) result_json.files 清单 —— 整包任务按季集匹配
            if res.get("mode") in ("bulk", "picks") \
                    or t.get("mode") in ("whole", "picks") \
                    or "files" in res:
                for entry in res.get("files") or []:
                    name = entry.get("name") or ""
                    fid = entry.get("fid") or ""
                    if not fid:
                        continue
                    if episode is None:
                        return CloudFile(fid=fid, name=name, size=0, is_dir=False)
                    if episode_of(name) != episode:
                        continue
                    p = parse(name)
                    if season is not None and p.season is not None \
                            and p.season != season:
                        continue
                    return CloudFile(fid=fid, name=name, size=0, is_dir=False)
            # 3) result_json.name（整包顶层文件/目录名）
            name = res.get("name") or ""
            fid = res.get("fid") or ""
            if name and fid:
                return CloudFile(fid=fid, name=name, size=0, is_dir=False)
        return None

    async def _search_all(self, provider: CloudDriveProvider, meta: VideoMeta,
                          video_id: str, user_id: str | None = None) -> list[CloudFile]:
        """多关键词搜索网盘并按 fid 去重：TMDB 双语名（跨语言）
        + 已完成转存任务的定位文件名（转存文件名常与标题语言不一致）。"""
        imdb_id, season, episode = parse_video_id(video_id)
        # TMDB 双语名跨语言检索：中文名与外语名都参与（网盘文件命名语言不定）
        keywords = [meta.title, meta.name_zh, meta.name_en]
        for t in db.get_tasks_for_video(self.db_path, vtype=meta.type,
                                        imdb_id=imdb_id, season=season,
                                        episode=episode, user_id=user_id):
            if t["status"] != "COMPLETED" or not t["result_json"]:
                continue
            try:
                res = json.loads(t["result_json"]) or {}
            except ValueError:
                continue
            name = res.get("name") or ""
            if name:
                keywords.append(name)
            # 整包任务落盘清单：命中目标集数的文件名也参与检索（裸集数
            # 文件名如 06.mkv 搜不到标题，是裸集数场景唯一可搜的关键词）
            for entry in res.get("files") or []:
                fname = entry.get("name") or ""
                if fname and episode is not None \
                        and episode_of(fname) == episode:
                    keywords.append(fname)
        seen: dict[str, CloudFile] = {}
        for kw in dict.fromkeys(k for k in keywords if k):
            try:
                for f in await provider.search_files(kw):
                    seen.setdefault(f.fid, f)
            except ProviderError as e:
                log.warning("网盘搜索“%s”失败: %s", kw, e)
        return list(seen.values())

    def _with_guidance(self, vtype: str, video_id: str, reason: str,
                       user_id: str | None = None) -> list[dict]:
        """未命中时附加 externalUrl 引导流（Stremio 标准字段，点击在浏览器打开），
        不伪造播放流。已有转存任务时指向任务页并展示实时进度（阶段 4 播放闭环：
        转存完成后用户在 Stremio 重新刷新片源即可命中播放）。"""
        if not config.BASE_URL:
            return []
        url = f"{config.BASE_URL}/search?vtype={vtype}&id={video_id}"
        extra = "。点击打开候选资源页，选择资源后转存到网盘。"
        imdb_id, season, episode = parse_video_id(video_id)
        tasks = db.get_tasks_for_video(self.db_path, vtype, imdb_id, season,
                                       episode, user_id=user_id)
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
        info = name_parser.parse(resource.filename)
        quality = info.resolution or ""
        hints = {
            "notWebReady": True,  # 客户端直连 CDN，需携带请求头（Web 端不支持）
            "bingeGroup": f"{resource.source_name}-{info.title or 'video'}",
            "filename": resource.filename,
            "videoSize": resource.video_size,
            "proxyHeaders": {"request": resource.headers},
        }
        spec_line = _spec_emoji_line(info)
        desc = f"{reason}\n{spec_line}\n{resource.filename}" if spec_line \
            else f"{reason}\n{resource.filename}"
        return {
            "url": resource.url,
            "name": f"{resource.source_name}\n{quality}".rstrip(),
            "title": resource.filename,
            "description": desc,
            "behaviorHints": hints,
        }
