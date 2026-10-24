"""候选资源搜索编排：元数据 → 关键词 → 多来源并发检索 → 去重评分。

评分依据（需求第 5 条"去重并为资源匹配度提供评分依据"）：
- 主体：候选标题与影片标题的归一化相似度（difflib.SequenceMatcher）；
- 加分：标题完整包含影片名、年份一致、（剧集）季数匹配；
- 减分：垃圾资源词（预告/花絮/预告片等）、（剧集）明确标注了其他季数；
- 次级排序：发布时间新者优先。
"""
import asyncio
import logging
import os
import re
from datetime import datetime
from difflib import SequenceMatcher

from . import config, db
from .tmdb import VideoMeta, fetch_meta
from .stream_service import parse_video_id
from .matcher import JUNK_PATTERN, VIDEO_EXTS, episode_of, parse, parse_file_spec
from .providers.base import ProviderError
from .providers.quark import QuarkDriveProvider
from .providers.search import PanSouProvider, ResourceSearchProvider, SearchCandidate, SearchError
from .transfer_worker import parse_pwd_id

log = logging.getLogger(__name__)

_JUNK_RE = re.compile(r"trailer|teaser|sample|featurette|花絮|预告|样片|片段", re.IGNORECASE)
_SEASON_RE = re.compile(r"[sS](\d{1,2})\b|第\s*(\d{1,2})\s*季")
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _human_size(n: int) -> str:
    for unit, div in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n}B"


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s.lower())


def _season_of(note: str) -> int | None:
    m = _SEASON_RE.search(note)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def score_candidate(cand: SearchCandidate, meta: VideoMeta, season: int | None,
                    keyword: str = "") -> float:
    """0~1 量级的匹配评分（仅用于排序展示）。

    相似度同时参考实际搜索关键词与 TMDB 原标题（跨语言场景下候选
    资源标题可能与原标题语言不同，与关键词同语言时以关键词为准）。
    """
    note = _norm(cand.title)
    ref = max(
        (_norm(x) for x in (keyword, meta.title) if x),
        key=lambda t: SequenceMatcher(None, note, t).ratio() if note else 0.0,
        default="",
    )
    score = 0.0
    if note and ref:
        score = max(
            SequenceMatcher(None, note, ref).ratio(),
            0.75 if ref in note else 0.0,  # 完整包含片名：相似度对短标题偏保守
        )
    if _JUNK_RE.search(cand.title):
        score -= 0.3
    if season is not None:
        cand_season = _season_of(cand.title)
        if cand_season == season:
            score += 0.1
        elif cand_season is not None and cand_season != season:
            score -= 0.15  # 明确标注了其他季
    if meta.year:
        years = {int(y) for y in _YEAR_RE.findall(cand.title)}
        if meta.year in years:
            score += 0.05
    return round(max(score, 0.0), 3)


def _published_ts(cand: SearchCandidate) -> float:
    if not cand.published:
        return 0.0
    try:
        return datetime.fromisoformat(cand.published).timestamp()
    except ValueError:
        return 0.0


class CandidateService:
    """聚合多个 ResourceSearchProvider 的检索编排。"""

    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path

    def _providers(self) -> list[ResourceSearchProvider]:
        providers: list[ResourceSearchProvider] = []
        if config.PANSOU_BASE_URL:
            providers.append(PanSouProvider(config.PANSOU_BASE_URL,
                                            timeout=config.SEARCH_TIMEOUT))
        return providers

    async def search(self, vtype: str, video_id: str, keyword: str = "") \
            -> tuple[VideoMeta | None, list[SearchCandidate], str, str]:
        """返回 (影片元数据, 候选列表, 错误说明, 实际使用的关键词)。

        关键词来源：手动指定 > TMDB 中文名 > TMDB 外语名（归一化去重，手动关键词
        不做缓存）。影片同时有中文名与外语名时，两个名字各搜一次并按链接去重合并。
        来源级失败不中断整体搜索。候选合并后统一做链接存活检测（PanSou
        /api/check/links），失效与不确定的条目排序沉底，检测结果填充到
        alive_state/alive_summary。
        """
        imdb_id, season, _ = parse_video_id(video_id)
        meta = await fetch_meta(imdb_id, vtype, self.db_path)
        if meta is None or not meta.title:
            return None, [], f"TMDB 未收录 {video_id}", keyword
        names = [keyword.strip(), meta.name_zh, meta.name_en]
        keywords: list[str] = []
        seen: set[str] = set()
        for n in names:
            if n and (nn := _norm(n)) not in seen:
                seen.add(nn)
                keywords.append(n)
        kw = keywords[0] if keywords else meta.title
        providers = self._providers()
        if not providers:
            return meta, [], "未配置任何外部搜索来源", kw
        tasks = [(p, k) for p in providers for k in keywords]
        results = await asyncio.gather(
            *(p.search(k, limit=config.SEARCH_LIMIT) for p, k in tasks),
            return_exceptions=True,
        )
        candidates: list[SearchCandidate] = []
        errors: list[str] = []
        for (p, k), res in zip(tasks, results):
            if isinstance(res, SearchError):
                if f"{p.name}: {res}" not in errors:
                    errors.append(f"{p.name}: {res}")
            elif isinstance(res, BaseException):
                errors.append(f"{p.name}: 内部错误")
                log.error("%s 搜索异常", p.name, exc_info=res)
            else:
                for cand in res:
                    cand.score = score_candidate(cand, meta, season, k)
                    candidates.append(cand)
            await p.close()
        # 跨来源/跨关键词去重：同链接保留得分高者；先按匹配度排序，再做存活检测
        best: dict[str, SearchCandidate] = {}
        for cand in candidates:
            cur = best.get(cand.share_url)
            if cur is None or cand.score > cur.score:
                best[cand.share_url] = cand
        merged = sorted(best.values(),
                        key=lambda c: (-c.score, -_published_ts(c)))
        # 存活检测：失效与不确定的条目沉底（组内仍按 匹配度→发布时间 排序）
        if merged:
            checks = await self.verify_links([{"share_url": c.share_url,
                                               "passcode": c.passcode}
                                              for c in merged])
            by_url = {r["share_url"]: r for r in checks}
            for c in merged:
                r = by_url.get(c.share_url) or {}
                c.alive_state = r.get("state") or "uncertain"
                c.alive_summary = r.get("summary") or ""
            merged.sort(key=lambda c: (0 if c.alive_state in ("ok", "locked") else 1,
                                       -c.score, -_published_ts(c)))
        log.info("候选搜索 %s kw=%r: %d 关键词 %d 来源 %d 有效候选, 错误: %s",
                 video_id, kw, len(keywords), len(providers), len(merged),
                 errors or "无")
        return meta, merged, "；".join(errors), kw

    async def verify_links(self, items: list[dict]) -> list[dict]:
        """批量检测候选分享链接存活状态（PanSou /api/check/links）。

        items: [{"share_url", "passcode"}]，返回与输入同序的
        [{"share_url", "state", "summary"}]，state ∈ ok/bad/locked/unsupported/uncertain。
        检测由 PanSou 服务端代查（自带结果缓存），失败整体降级 uncertain 不抛出。
        """
        if not items:
            return []
        provider = PanSouProvider(config.PANSOU_BASE_URL,
                                  timeout=config.SEARCH_TIMEOUT)
        payload = []
        for it in items:
            entry = {"disk_type": "quark", "url": it["share_url"]}
            if it.get("passcode"):
                entry["password"] = it["passcode"]
            payload.append(entry)
        try:
            results = await provider.check_links(payload)
        except SearchError as e:
            log.warning("候选存活检测失败，全部标记 uncertain: %s", e)
            return [{"share_url": it["share_url"], "state": "uncertain",
                     "summary": "检测服务不可用"} for it in items]
        finally:
            await provider.close()
        by_url = {r.get("url"): r for r in results if r.get("url")}
        out = []
        for it in items:
            r = by_url.get(it["share_url"]) or {}
            out.append({"share_url": it["share_url"],
                        "state": r.get("state") or "uncertain",
                        "summary": r.get("summary") or "未获取检测结果"})
        return out

    async def inspect_share(self, share_url: str, passcode: str = "") -> dict:
        """解析分享链接的内容清单与文件规格（阶段 5，实时查询夸克）。

        返回 {"video_count", "other_count", "total_size", "total_size_h",
        "is_series", "seasons", "files"}；files 每项含 name/path/fid/size/
        size_h/season/episode/spec（清晰度/编码/音轨/字幕/语言，来自文件名标注）。
        分享失效/提取码错误等抛 ProviderError。
        """
        cookie = db.load_cookie(self.db_path) or (config.QUARK_COOKIE or None)
        if not cookie:
            raise ProviderError("未绑定网盘账号，请访问 /setup 完成授权")
        provider = QuarkDriveProvider(cookie=cookie, db_path=self.db_path)
        try:
            pwd_id = parse_pwd_id(share_url)
            stoken = await provider.get_share_stoken(pwd_id, passcode)
            entries = await provider.list_share_files(pwd_id, stoken)
        finally:
            await provider.close()

        files: list[dict] = []
        other_count = 0
        for e in entries:
            if e.is_dir:
                continue
            if os.path.splitext(e.name)[1].lower() not in VIDEO_EXTS \
                    or JUNK_PATTERN.search(e.name):
                other_count += 1
                continue
            p = parse(e.name)
            files.append({
                "name": e.name, "path": e.path, "fid": e.fid, "size": e.size,
                "size_h": _human_size(e.size),
                "season": p.season, "episode": p.episode or episode_of(e.name),
                "spec": parse_file_spec(e.name),
            })
        files.sort(key=lambda f: ((f["season"] or 99), (f["episode"] or 999), f["name"]))
        total_size = sum(f["size"] for f in files)
        seasons: dict[int, dict] = {}
        for f in files:
            if f["season"] is not None:
                s = seasons.setdefault(f["season"], {"count": 0, "size": 0})
                s["count"] += 1
                s["size"] += f["size"]
        return {
            "video_count": len(files),
            "other_count": other_count,
            "total_size": total_size,
            "total_size_h": _human_size(total_size),
            "is_series": any(f["season"] is not None or f["episode"] is not None
                             for f in files),
            "seasons": [{"season": s, "count": v["count"],
                         "size_h": _human_size(v["size"])}
                        for s, v in sorted(seasons.items())],
            "files": files,
        }
