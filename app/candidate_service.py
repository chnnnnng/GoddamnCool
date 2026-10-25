"""候选资源搜索编排：元数据 → 关键词 → 多来源并发检索 → 去重评分。

评分依据（需求第 5 条"去重并为资源匹配度提供评分依据"）：
- 主体：候选标题与影片标题的归一化相似度（difflib.SequenceMatcher）；
- 加分：标题完整包含影片名、年份一致、（剧集）季数匹配；
- 减分：垃圾资源词（预告/花絮/预告片等）、（剧集）明确标注了其他季数；
- 次级排序：发布时间新者优先。
"""
import asyncio
import logging
import time
import os
import re
from datetime import datetime
from difflib import SequenceMatcher

from . import config, db, settings
from .name_parser import parser as name_parser, spec_dict
from .tmdb import VideoMeta, fetch_meta
from .stream_service import parse_video_id
from .matcher import JUNK_PATTERN, VIDEO_EXTS
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


def score_candidate(cand: SearchCandidate, meta: VideoMeta | None,
                    season: int | None, keyword: str = "") -> float:
    """0~1 量级的匹配评分（仅用于排序展示）。

    相似度同时参考实际搜索关键词与 TMDB 原标题（跨语言场景下候选
    资源标题可能与原标题语言不同，与关键词同语言时以关键词为准）；
    独立搜索（无影片元数据）时仅按关键词评分。
    """
    note = _norm(cand.title)
    ref = max(
        (_norm(x) for x in (keyword, meta.title if meta else "") if x),
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
    if meta and meta.year:
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
        if settings.get("PANSOU_BASE_URL"):
            providers.append(PanSouProvider(settings.get("PANSOU_BASE_URL"),
                                            timeout=config.SEARCH_TIMEOUT))
        return providers

    async def search(self, vtype: str, video_id: str, keyword: str = "",
                     disks: list[str] | None = None, cookie: str = "") \
            -> tuple[VideoMeta | None, list[SearchCandidate], str, str]:
        """返回 (影片元数据, 候选列表, 错误说明, 实际使用的关键词)。

        video_id 非空（Stremio 锚定模式）：关键词来源为 手动指定 > TMDB 中文名 >
        TMDB 外语名（归一化去重），影片同时有中文名与外语名时两个名字各搜一次
        并按链接去重合并；video_id 为空（独立搜索模式）：跳过 TMDB，仅按手动
        关键词搜索一次。
        TMDB key 由管理员统一配置（settings），普通用户不可见。
        disks: 网盘类型列表；None → 默认仅夸克，空列表 → 全部网盘（透传 Provider）。
        cookie: 调用者自己绑定的网盘 Cookie（多用户隔离），用于"有效"候选的
        分享内容解析；缺省时内容解析跳过（详情不展示、不排除无视频条目）。
        来源级失败不中断整体搜索。候选合并后统一对夸克链接做存活检测（PanSou
        /api/check/links），并对"有效"候选解析分享内容：详情填充到 detail
        （搜索结果页直显），内容中无视频文件的标记为 bad 排除出"有效"。
        结果缓存（kv_cache，TTL 默认 1 天）：命中时不再请求外部来源。
        "网盘中已有 N 项"不在本层计算（detail 可能来自 1 天缓存），由路由层
        组装响应时实时补算。
        """
        imdb_id, season, _ = parse_video_id(video_id or "")
        meta = await fetch_meta(imdb_id, vtype, self.db_path) if imdb_id else None
        if imdb_id and (meta is None or not meta.title):
            return None, [], f"TMDB 未收录 {video_id}", keyword
        names = [keyword.strip()] + ([meta.name_zh, meta.name_en] if meta else [])
        keywords: list[str] = []
        seen: set[str] = set()
        for n in names:
            if n and (nn := _norm(n)) not in seen:
                seen.add(nn)
                keywords.append(n)
        kw = keywords[0] if keywords else (meta.title if meta else "")
        if not keywords:
            return meta, [], "请输入搜索关键词", kw
        providers = self._providers()
        if not providers:
            return meta, [], "未配置任何外部搜索来源", kw
        # 结果缓存（需求 7：搜索结果缓存 1 天）。key 含有效关键词与网盘类型；
        # 仅缓存无来源级错误的成功结果（避免把瞬时故障的空结果缓存一天）。
        disks_key = "all" if disks == [] else (",".join(disks) if disks else "default")
        cache_key = (f"search:{vtype}|{video_id or ''}|{'>'.join(keywords)}"
                     f"|{disks_key}")
        # 缓存读写移到 chain 层（BaseSearchChain/AISearchChain 统一管
        # 普通/AI 两种缓存，这里只负责 PanSou + verify_links + 去重）
        tasks = [(p, k) for p in providers for k in keywords]
        t0_svc = time.monotonic()
        results = await asyncio.gather(
            *(p.search(k, limit=settings.get("SEARCH_LIMIT"), cloud_types=disks)
              for p, k in tasks),
            return_exceptions=True,
        )
        # 记录每个 provider.search 调用（PanSou 层面）
        dur_svc_ms = int((time.monotonic() - t0_svc) * 1000)
        for (p, k), res in zip(tasks, results):
            ok = not isinstance(res, (SearchError, BaseException))
            db.record_api_call(self.db_path, kind="pansou", endpoint=f"search.{p.name}",
                               duration_ms=dur_svc_ms // max(1, len(tasks)),
                               success=ok,
                               error=str(res)[:200] if isinstance(res, Exception) else "",
                               meta={"provider": p.name, "keyword": k[:50],
                                     "limit": settings.get("SEARCH_LIMIT")})
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
        # 存活检测（仅夸克链接）：失效与不确定的沉底；非夸克未检测居中
        if merged:
            quark = [c for c in merged if c.disk == "quark"]
            if quark:
                checks = await self.verify_links([{"share_url": c.share_url,
                                                   "passcode": c.passcode}
                                                  for c in quark])
                by_url = {r["share_url"]: r for r in checks}
                for c in quark:
                    r = by_url.get(c.share_url) or {}
                    c.alive_state = r.get("state") or "uncertain"
                    c.alive_summary = r.get("summary") or ""
            # 【重要】：不在此处 inspect_share
            # inspect_share 延后到 chain 里按需调（BaseSearchChain/AISearchChain），
            # 只对 TOP INSPECT_TOP_N 的 alive 候选做普通解析，节约 RPC。
            # merged 仍按 score 降序 + 匹配度分档（有效候选自然在前面）。
            merged.sort(key=lambda c: (2 if c.alive_state in ("bad", "unsupported") else
                                       1 if c.alive_state == "uncertain" else 0,
                                       -c.score, -_published_ts(c)))
        log.info("候选搜索 %s kw=%r: %d 关键词 %d 来源 %d 有效候选, 错误: %s",
                 video_id or "(独立搜索)", kw, len(keywords), len(providers),
                 len(merged), errors or "无")
        # 缓存由 chain 层统一管理（BaseSearchChain/AISearchChain）
        return meta, merged, "；".join(errors), kw

    @staticmethod
    def _cand_dict(c: SearchCandidate) -> dict:
        return {"title": c.title, "share_url": c.share_url,
                "passcode": c.passcode, "source": c.source,
                "published": c.published, "provider": c.provider,
                "disk": c.disk, "score": c.score,
                "alive_state": c.alive_state, "alive_summary": c.alive_summary,
                "detail": c.detail}

    @staticmethod
    def _resort(merged: list[SearchCandidate]) -> None:
        """存活三组排序：有效/需提取码 → 未检测 → 失效/不确定；组内按匹配度→时间。"""
        merged.sort(key=lambda c: (2 if c.alive_state in ("bad", "uncertain",
                                                          "unsupported") else
                                   1 if not c.alive_state else 0,
                                   -c.score, -_published_ts(c)))

    async def _enrich_ok(self, quark: list[SearchCandidate],
                         cookie: str = "") -> None:
        """对"有效"的夸克候选并发解析分享内容（inspect 结果自带 1 天缓存）：
        - detail 填充到候选（搜索结果页直显季集/规格摘要）；
        - 内容中无视频文件 → 标记 bad（"内容无视频文件"），排挤出"有效"；
        - 单条解析失败不影响整体（保持原存活状态）。"""
        targets = [c for c in quark if c.alive_state == "ok"][:settings.get("INSPECT_TOP_N")]
        if not targets:
            return
        sem = asyncio.Semaphore(4)

        async def one(c: SearchCandidate) -> None:
            async with sem:
                try:
                    c.detail = await self.inspect_share(c.share_url, c.passcode,
                                                        cookie=cookie)
                except Exception as e:  # 解析失败保守保留原状态
                    log.info("候选内容解析失败 %s: %s", c.share_url, e)

        await asyncio.gather(*(one(c) for c in targets))
        for c in targets:
            if c.detail is not None and c.detail.get("video_count", 0) == 0:
                # 分享链接 PanSou 检测为"活"但内容里没有视频文件（只有杂项/海报/压缩包等）
                # → 保留"有效"里不好区分，也不该进"失效"，归到"未检测"这一档让前端把它
                #    当成没有可用视频的候选展示（不再出现在主按钮候选里）。
                c.alive_state = "uncertain"
                c.alive_summary = "内容无视频文件"

    async def verify_links(self, items: list[dict]) -> list[dict]:
        """批量检测候选分享链接存活状态（PanSou /api/check/links）。

        items: [{"share_url", "passcode"}]，返回与输入同序的
        [{"share_url", "state", "summary"}]，state ∈ ok/bad/locked/unsupported/uncertain。
        检测由 PanSou 服务端代查（自带结果缓存），失败整体降级 uncertain 不抛出。
        """
        if not items:
            return []
        provider = PanSouProvider(settings.get("PANSOU_BASE_URL"),
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

    async def inspect_share(self, share_url: str, passcode: str = "",
                            cookie: str = "") -> dict:
        """解析分享链接的内容清单与文件规格（阶段 5，实时查询夸克）。

        cookie: 调用者自己绑定的网盘 Cookie（多用户隔离，必传）。
        返回 {"video_count", "other_count", "total_size", "total_size_h",
        "is_series", "seasons", "files"}；files 每项含 name/path/fid/size/
        size_h/season/episode/spec（清晰度/编码/音轨/字幕/语言，来自文件名标注）。
        分享失效/提取码错误等抛 ProviderError。结果缓存（TTL 默认 1 天）。
        """
        cache_key = f"inspect:{share_url}|{passcode}"
        if (cached := db.get_cache(self.db_path, cache_key)) is not None:
            return cached
        if not cookie:
            raise ProviderError("未绑定网盘 Cookie，请在控制面板绑定")
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
            # 季集/规格统一由 MessyNameParser 解析（覆盖 SE03.11/第X集/狂飙01
            # 等乱七八糟命名，比旧 matcher 更准）；spec 为展示规格字典
            info = name_parser.parse(e.name)
            files.append({
                "name": e.name, "path": e.path, "fid": e.fid, "size": e.size,
                "size_h": _human_size(e.size),
                "season": info.season, "episode": info.episode,
                "spec": spec_dict(info),
            })
        files.sort(key=lambda f: ((f["season"] or 99), (f["episode"] or 999), f["name"]))
        total_size = sum(f["size"] for f in files)
        seasons: dict[int, dict] = {}
        for f in files:
            if f["season"] is not None:
                s = seasons.setdefault(f["season"], {"count": 0, "size": 0})
                s["count"] += 1
                s["size"] += f["size"]
        data = {
            "video_count": len(files),
            "other_count": other_count,
            "total_size": total_size,
            "total_size_h": _human_size(total_size),
            "is_series": any(f["season"] is not None or f["episode"] is not None
                             for f in files),
            "seasons": [{"season": s, "count": v["count"],
                         "size_h": _human_size(v["size"])}
                        for s, v in sorted(seasons.items())],
            "files": files[:80],  # 缓存/展示上限，防超大分享撑爆响应
        }
        db.set_cache(self.db_path, cache_key, data, settings.get("SEARCH_CACHE_TTL"))
        return data
