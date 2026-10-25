"""搜索资源页与转存任务 API（前后端分离）。

页面为 web/ 下静态文件（search.html / task.html），本模块只提供 JSON API
与页面路由；所有 /api/* 接口要求登录（X-Session-Token 会话）。

两种搜索模式（/search 页面按 URL 参数自动区分）：
- Stremio 锚定模式：/search?vtype=movie&id=tt...（带影片，自动搜 TMDB 双语名，
  转存任务锚定影片，完成后 Stream 自动命中）；
- 独立搜索模式：/search（仅手动关键词 + 网盘类型筛选，转存为 share-only 任务）。

多用户隔离（阶段 8）：
- 搜索/inspect/转存一律使用登录用户自己绑定的网盘 Cookie，TMDB key 管理员统一配置；
- Lite 用户每日搜索有配额（LITE_DAILY_SEARCH_LIMIT），Pro/管理员不限；
- 转存任务按用户隔离，任务详情仅本人与管理员可见。

安全要点：
- 第三方候选标题在前端一律 textContent 渲染，防存储型 XSS；
- 确认转存时服务端校验分享链接格式（仅夸克官方分享域，防 SSRF/任意 URL）；
- 任务 ID 为 uuid4 hex，防猜测遍历；影片标题以服务端 TMDB 结果为准。
"""
import json
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config, db, llm, settings
from .auth import require_user
from .matcher import _title_keys_similarity
from .name_parser import canonical_with_context, spec_dict
from .name_parser import parser as name_parser
from .search_chains import BaseSearchChain, AISearchChain  # 需求第 3 条：普通链/AI 链
from .tmdb import fetch_meta
from .providers.search import SHARE_URL_RE
from .providers.quark import QuarkDriveProvider
from .candidate_service import CandidateService
from .stream_service import parse_video_id
from .web_routes import page

router = APIRouter()
candidate_service = CandidateService()

_STATUS_TEXT = {
    "PENDING": "等待执行",
    "SUBMITTING": "正在提交转存",
    "PROCESSING": "转存处理中",
    "COMPLETED": "转存完成",
    "FAILED": "转存失败",
    "EXPIRED": "任务超时或失效",
}

# 独立搜索已知网盘类型（PanSou merged_by_type 键；前端 chips 与服务端校验共用）
KNOWN_DISKS = ("quark", "uc", "baidu", "aliyun", "xunlei", "115", "123", "tianyi")


def _parse_disks(raw: str) -> list[str] | None:
    """disks 参数解析（与 Provider 语义对齐）：
    空 → None（默认仅夸克）；"all" → []（不限类型搜全部）；逗号分隔 → 校验后的列表。"""
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    if raw == "all":
        return []
    disks = [d for d in (x.strip() for x in raw.split(",")) if d]
    unknown = [d for d in disks if d not in KNOWN_DISKS]
    if unknown or not disks:
        raise ValueError(f"不支持的网盘类型: {','.join(unknown)}")
    return disks


def _unlimited(user: dict) -> bool:
    """该用户是否不受每日搜索/转存配额限制（Pro 与管理员不限）。"""
    return user["role"] == "admin" or user["tier"] == "pro"


# 网盘已有文件索引缓存 {user_id: (拉取时间, _ExistingIndex)}；TTL 60s——
# 转存/改名后最多延迟 1 分钟反映到"网盘中已有 N 项"，避免每次搜索都全盘递归
_DRIVE_EXISTING_TTL = 60.0
_drive_existing_cache: dict[str, tuple[float, "_ExistingIndex"]] = {}


class _ExistingIndex:
    """用户网盘已有文件索引（需求第 9 条）。

    转存会智能重命名为规范名，"已在网盘"不能再用同名判断：
    1. 同名快速集合（未改名/手动整理场景）；
    2. 精确文件大小匹配 + 粗略影片名匹配（规范名场景，大小→文件倒排）。
    网盘文件名解析按需惰性缓存，避免整盘预解析。"""

    def __init__(self, files: list):
        self.names = {f.name for f in files}
        self.by_size: dict[int, list] = {}
        for f in files:
            if f.size:
                self.by_size.setdefault(int(f.size), []).append(f)
        self._memo: dict[str, object] = {}

    def _parse(self, name: str):
        if name not in self._memo:
            self._memo[name] = name_parser.parse(name)
        return self._memo[name]

    def match(self, name: str, size: int | None = 0) -> bool:
        """分享文件是否已在网盘：同名，或（大小精确相等 + 粗略片名匹配）。"""
        base = os.path.basename(name or "")
        if base and base in self.names:
            return True
        if not size or not base:
            return False
        p = self._parse(base)
        ta = p.title or p.title_en
        if not ta:
            return False  # 纯季集/编号名无从粗略匹配
        for f in self.by_size.get(int(size), []):
            q = self._parse(f.name)
            tb = q.title or q.title_en
            if not tb or _title_keys_similarity(ta, tb) < 0.75:
                continue
            if p.episode is not None and q.episode is not None \
                    and p.episode != q.episode:
                continue
            if p.season is not None and q.season is not None \
                    and p.season != q.season:
                continue
            return True
        return False

    def __bool__(self) -> bool:
        return bool(self.names or self.by_size)


async def _drive_existing_index(cookie: str, user_id: str) -> _ExistingIndex:
    """拉取用户网盘全部文件（list_all_files 递归列取，含大小），失败返回空索引。"""
    if not cookie:
        return _ExistingIndex([])
    now = time.monotonic()
    hit = _drive_existing_cache.get(user_id)
    if hit and now - hit[0] < _DRIVE_EXISTING_TTL:
        return hit[1]
    provider = QuarkDriveProvider(cookie=cookie, db_path=config.DB_PATH,
                                  user_id=user_id)
    try:
        index = _ExistingIndex(await provider.list_all_files())
    except Exception:  # 网盘列名失败不阻塞搜索，仅无提示
        return _ExistingIndex([])
    finally:
        await provider.close()
    _drive_existing_cache[user_id] = (now, index)
    return index


@router.get("/search")
async def search_page():
    return page("search.html")


@router.get("/candidates")
async def candidates_page():
    """兼容旧入口（Stremio externalUrl / 旧书签）：与 /search 同页。"""
    return page("search.html")


@router.get("/api/candidates/search")
async def candidates_search(request: Request, vtype: str = "movie", id: str = "",
                            kw: str = "", disks: str = ""):
    user, err = require_user(request)
    if err:
        return err
    try:
        disk_list = _parse_disks(disks)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    if not id and not kw.strip():
        return JSONResponse({"detail": "请输入搜索关键词"}, status_code=400)
    # 每日配额：所有用户都计次（面板统计展示真实用量），
    # 仅 Lite 用户在超限时 429 拒绝（Pro/管理员不限额）
    if not _unlimited(user):
        limit = settings.get("LITE_DAILY_SEARCH_LIMIT")
        if db.get_search_count(config.DB_PATH, user["id"]) >= limit:
            return JSONResponse(
                {"detail": f"今日搜索次数已用完（{limit}/{limit}），升级 Pro 解除限制"},
                status_code=429)
    db.incr_search_count(config.DB_PATH, user["id"])
    user_cookie = db.load_user_cookie(config.DB_PATH, user["id"]) or ""
    # 需求第 3 条：普通链 vs AI 链——后端一体化，前端无感
    chain = AISearchChain() if llm.configured() else BaseSearchChain()
    season = None
    if id and vtype == "series":
        # 锚定模式：从 URL 参数或 TMDB 推断季数
        season = 1  # 默认 S01
    result = await chain.search(kw, vtype=vtype, id=id, season=season,
                                disks=disk_list, cookie=user_cookie)
    meta, cands, errors, keyword = (result["meta"], result["cands"],
                                     result["errors"], result["keyword"])
    # "网盘中已有 N 项"实时补算
    existing = await _drive_existing_index(user_cookie, user["id"])
    for c in cands:
        d = getattr(c, "detail", None)
        if not (d and d.get("files")):
            continue
        for f in d["files"]:
            nm = f.get("name") or ""
            if existing:
                f["exists"] = existing.match(nm, f.get("size") or 0)
            # canonical 已由链类算好（AI 链用 AI 元数据拼，Normal 链用规则），
            # 此处兜底防止链类漏算
            f.setdefault("canonical", canonical_with_context(
                nm, search_title=keyword or "",
                share_title=getattr(c, "title", "") or "",
                folder=_folder_of(f.get("path") or "")))
            _record_unparsed(nm, "search")
        if existing:
            d["existing_count"] = sum(
                1 for f in d["files"] if f.get("exists"))
    if id and meta is None:  # 锚定模式影片元数据失败（独立搜索 meta 恒为 null）
        return JSONResponse({"detail": errors or "影片元数据获取失败"}, status_code=404)
    return {
        "meta": ({"id": meta.id, "title": meta.title, "year": meta.year,
                  "type": meta.type} if meta else None),
        "keyword": keyword,
        "llm_enabled": llm.configured(),  # AI 精选入口（管理员配置 LLM 后开启）
        "candidates": [{
            "title": c.title, "share_url": c.share_url, "passcode": c.passcode,
            "source": c.source, "published": c.published,
            "provider": c.provider, "disk": c.disk, "score": c.score,
            "alive_state": c.alive_state, "alive_summary": c.alive_summary,
            "detail": c.detail,  # 有效候选的内容摘要（直显）；完整文件列表在 modal 用
        } for c in cands],
        "errors": errors,
    }


@router.post("/api/candidates/confirm")
async def candidates_confirm(request: Request):
    user, err = require_user(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    vtype = body.get("vtype", "")
    video_id = body.get("id", "")
    share_url = (body.get("share_url") or "").strip()
    # vtype 与 id 必须成对：都为空 → 独立转存；都非空 → Stremio 锚定转存
    if (bool(vtype)) != (bool(video_id)) or (vtype and vtype not in ("movie", "series")):
        return JSONResponse({"detail": "vtype/id 参数无效"}, status_code=400)
    # 分享链接服务端强校验：仅接受夸克官方分享域（需求第 10 条）
    if not SHARE_URL_RE.match(share_url):
        return JSONResponse({"detail": "分享链接无效（仅支持 pan.quark.cn 分享链接）"},
                            status_code=400)
    mode = body.get("mode") or "episode"
    if mode not in ("episode", "whole", "picks"):
        return JSONResponse({"detail": "mode 参数无效（episode/whole/picks）"},
                            status_code=400)
    # picks 模式：所选文件 fid 列表（多选转存），1..50 个非空短字符串
    picks: list[str] | None = None
    if mode == "picks":
        raw = body.get("fids")
        if not isinstance(raw, list):
            return JSONResponse({"detail": "picks 模式必须提供 fids 列表"},
                                status_code=400)
        picks = [str(f).strip() for f in raw if str(f).strip()][:50]
        if not picks or any(len(f) > 64 for f in picks):
            return JSONResponse({"detail": "fids 列表无效"}, status_code=400)
    if video_id:
        # Stremio 锚定模式：季集来自影片 ID，标题以 TMDB 为准（key 管理员统一配置）
        imdb_id, season, episode = parse_video_id(video_id)
        meta = await fetch_meta(imdb_id, vtype, candidate_service.db_path)
        if meta is None or not meta.title:
            return JSONResponse({"detail": "影片元数据获取失败，请稍后重试"}, status_code=400)
        title = meta.title
    else:
        # 独立搜索模式：share-only 任务（不锚定影片），季集可选由 body 提供
        imdb_id, season, episode = "", None, None
        if mode == "episode" and (body.get("season") is not None
                                  or body.get("episode") is not None):
            try:
                season = int(body["season"]) if body.get("season") is not None else None
                episode = int(body["episode"]) if body.get("episode") is not None else None
            except (TypeError, ValueError):
                return JSONResponse({"detail": "season/episode 必须是整数"}, status_code=400)
        title = (body.get("title") or "").strip()[:100] or "外部资源"
    # whole（转存完整剧集）：任务不锚定季集，整包落盘；
    # picks（转存所选剧集，可多选）：任务不锚定季集，按 fid 列表转存；
    # episode 仅转存所选单集
    # Lite 每日转存配额：仅对"新任务"校验（幂等复用既有活跃任务不消耗次数）
    active = db.find_active_task(
        config.DB_PATH, share_url=share_url, imdb_id=imdb_id,
        season=None if mode in ("whole", "picks") else season,
        episode=None if mode in ("whole", "picks") else episode,
        picks=picks, user_id=user["id"])
    if active is None and not _unlimited(user):
        tlimit = settings.get("LITE_DAILY_TRANSFER_LIMIT")
        if db.get_transfer_count(config.DB_PATH, user["id"]) >= tlimit:
            return JSONResponse(
                {"detail": f"今日转存次数已用完（{tlimit}/{tlimit}），升级 Pro 解除限制"},
                status_code=429)
    task, created = db.create_task(
        config.DB_PATH, vtype=vtype, imdb_id=imdb_id,
        season=None if mode in ("whole", "picks") else season,
        episode=None if mode in ("whole", "picks") else episode,
        title=title,
        candidate_title=(body.get("title") or "")[:300],
        share_url=share_url,
        passcode=(body.get("passcode") or "")[:32],
        source=(body.get("source") or "")[:100],
        mode=mode, picks=picks, user_id=user["id"],
    )
    if created:  # 新任务计次（幂等复用既有活跃任务不消耗）；Pro/管理员不限额
        db.incr_transfer_count(config.DB_PATH, user["id"])
    return {"task_id": task["id"], "created": created, "status": task["status"]}


@router.post("/api/candidates/inspect")
async def candidates_inspect(request: Request):
    """解析分享链接内容清单与文件规格（大小/清晰度/音轨/字幕/季集结构）。

    实时查询夸克分享目录树并解析文件名规格，供候选详情展示与转存选择。"""
    user, err = require_user(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    url = (body.get("share_url") or "").strip()
    if not SHARE_URL_RE.match(url):
        return JSONResponse({"detail": "分享链接无效（仅支持 pan.quark.cn 分享链接）"},
                            status_code=400)
    # 解析分享目录需要登录网盘：一律使用本人 Cookie
    cookie = db.load_user_cookie(config.DB_PATH, user["id"])
    if not cookie:
        return JSONResponse({"detail": "请先在控制面板绑定网盘 Cookie"},
                            status_code=400)
    try:
        data = await candidate_service.inspect_share(
            url, (body.get("passcode") or "")[:32], cookie=cookie)
    except Exception as e:
        return JSONResponse({"detail": f"分享解析失败: {e}"}, status_code=400)
    # 规范文件名兜底：无标题名结合搜索名/分享名/文件夹名（inspect 无季集目标）
    ctx_kw = (body.get("kw") or "").strip()
    ctx_title = (body.get("title") or "").strip()[:300]  # 候选/分享标题（前端传）
    for f in data.get("files", []):
        nm = f.get("name") or ""
        # 规范文件名：无标题名结合搜索名/分享名/文件夹名兜底
        f["canonical"] = canonical_with_context(
            nm, search_title=ctx_kw, share_title=ctx_title,
            folder=_folder_of(f.get("path") or ""))
        _record_unparsed(nm, "search")
    # "网盘中已存在"标记（需求第 9 条）：精确大小匹配 + 粗略影片名匹配
    # （转存后文件已智能重命名为规范名，同名判断失效）
    data["existing_count"] = 0
    try:
        existing = await _drive_existing_index(cookie, user["id"])
        for f in data.get("files", []):
            f["exists"] = existing.match(f.get("name") or "",
                                         f.get("size") or 0)
        data["existing_count"] = sum(1 for f in data.get("files", [])
                                     if f.get("exists"))
    except Exception:  # 网盘列名失败不阻塞分享解析，仅无提示
        for f in data.get("files", []):
            f["exists"] = False
    return data


@router.post("/api/candidates/verify")
async def candidates_verify(request: Request):
    """批量检测候选分享链接存活状态（代理 PanSou /api/check/links）。

    前端列出候选后逐个调用本接口标注存活状态；非法链接直接跳过不送检。"""
    _, err = require_user(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    raw = body.get("items")
    if not isinstance(raw, list) or not raw:
        return JSONResponse({"detail": "items 不能为空"}, status_code=400)
    items = []
    for it in raw[:40]:  # 单次检测上限，防滥用
        if not isinstance(it, dict):
            continue
        url = (it.get("share_url") or "").strip()
        if not SHARE_URL_RE.match(url):
            continue  # 非法链接跳过（前端保持"未检测"）
        items.append({"share_url": url, "passcode": (it.get("passcode") or "")[:32]})
    if not items:
        return JSONResponse({"detail": "无有效的夸克分享链接"}, status_code=400)
    return {"results": await candidate_service.verify_links(items)}


@router.get("/api/llm-status")
async def llm_status(request: Request):
    """AI 功能可用性（搜索页据此把【搜索】按钮替换为【AI 精选】）。"""
    _, err = require_user(request)
    if err:
        return err
    return {"enabled": llm.configured()}


@router.post("/api/candidates/ai-review")
async def candidates_ai_review(request: Request):
    """AI 精选+解析（高级功能，需管理员配置 LLM API）：一次调用同时完成
    候选筛选（剔除无关/垃圾资源）、重排序（推荐分）、去重分组与逐文件
    影片信息解析（Title/SxxEyy/Year/Resolution/Source/Codec/Audio-Group）。

    有效候选可附带 files（分享内完整文件路径，仅文件名，不含用户网盘
    目录）；模型对每个文件给出规范名（canonical），服务端据此合并
    原名/规范名的规格（spec）后返回，前端直接用于徽标与规范名展示。
    结果按输入内容哈希缓存（TTL 1 天）。未配置 LLM 时返回 501。"""
    _, err = require_user(request)
    if err:
        return err
    if not llm.configured():
        return JSONResponse({"detail": "管理员未配置 LLM API，AI 功能不可用"},
                            status_code=501)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    raw = body.get("candidates")
    if not isinstance(raw, list) or not raw:
        return JSONResponse({"detail": "candidates 不能为空"}, status_code=400)
    # 输入规整为小而结构化的元数据（截断防注入与 token 膨胀），上限 30 条；
    # 仅有效候选（前端 inspect 过的）带 files，每候选最多 40 个文件
    cands: list[dict] = []
    for it in raw[:30]:
        if not isinstance(it, dict):
            continue
        vc = it.get("video_count")
        cand = {
            "title": str(it.get("title") or "")[:200],
            "source": str(it.get("source") or "")[:60],
            "published": str(it.get("published") or "")[:20],
            "alive_state": str(it.get("alive_state") or "")[:16],
            "video_count": vc if isinstance(vc, int)
                           and not isinstance(vc, bool) else None,
            "total_size_h": str(it.get("total_size_h") or "")[:20],
        }
        files = []
        for f in (it.get("files") or [])[:40]:
            if not isinstance(f, dict):
                continue
            path = str(f.get("path") or "").strip()[:200]
            if path:
                files.append({"path": path})
        if files:
            cand["files"] = files
        cands.append(cand)
    if not cands:
        return JSONResponse({"detail": "无有效的候选数据"}, status_code=400)
    season = body.get("season")
    if season is not None and (isinstance(season, bool)
                               or not isinstance(season, int)):
        return JSONResponse({"detail": "season 必须是整数"}, status_code=400)
    try:
        items = await llm.review_candidates(
            cands,
            keyword=str(body.get("keyword") or "")[:100],
            title=str(body.get("title") or "")[:200],
            season=season)
    except llm.LLMError as e:
        return JSONResponse({"detail": f"AI 处理失败: {e}"}, status_code=502)
    # 逐文件补充：规范名来源标记 + 合并原名/规范名的规格（徽标展示用，
    # 中文名常带 中英双字/Atmos 等字幕音轨信息，规范名可能带分辨率/来源）
    for it in items:
        cand = cands[it["index"]] if it["index"] < len(cands) else {}
        raw_files = cand.get("files") or []
        for pf in it.get("files") or []:
            i = pf.get("i")
            orig = raw_files[i]["path"] if isinstance(i, int) and i < len(raw_files) else ""
            pf["source"] = "ai" if pf.get("canonical") else None
            pf["spec"] = _spec_merge(orig, pf.get("canonical") or "")
    return {"items": items}


@router.post("/api/candidates/ai-parse")
async def candidates_ai_parse(request: Request):
    """AI 兜底解析（高级功能）：对规则解析不出标题的文件名，结合
    搜索关键词/分享标题/文件夹名上下文批量推断 影片名/季/集。

    输入仅文件名与其上下文（不含用户网盘目录）；结果缓存（TTL 1 天），
    并将文件名登记到解析问题名单（source=ai）供管理员跟进。"""
    _, err = require_user(request)
    if err:
        return err
    if not llm.configured():
        return JSONResponse({"detail": "管理员未配置 LLM API，AI 功能不可用"},
                            status_code=501)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    raw = body.get("names")
    if not isinstance(raw, list) or not raw:
        return JSONResponse({"detail": "names 不能为空"}, status_code=400)
    names = []
    for it in raw[:50]:  # 上限 50 个，控制单次调用 token 开销
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:300]
        if not name:
            continue
        names.append({
            "name": name,
            "search_title": str(it.get("search_title") or "")[:100],
            "share_title": str(it.get("share_title") or "")[:200],
            "folder": str(it.get("folder") or "")[:100],
        })
    if not names:
        return JSONResponse({"detail": "无有效的文件名"}, status_code=400)
    try:
        items = await llm.parse_names(names)
    except llm.LLMError as e:
        return JSONResponse({"detail": f"AI 处理失败: {e}"}, status_code=502)
    for n in names:  # 登记跟进名单（去重累计），管理员可见 AI 兜底结果覆盖面
        db.record_name_issue(config.DB_PATH, n["name"], "ai")
    return {"items": items}


@router.get("/task/{task_id}")
async def task_page(task_id: str):
    return page("task.html")


def _folder_of(path: str) -> str:
    """分享内文件所在文件夹名（路径最深一级目录，用于无标题文件名兜底）。"""
    d = os.path.dirname((path or "").replace("\\", "/")).strip("/")
    return d.rsplit("/", 1)[-1] if d else ""


def _record_unparsed(name: str, source: str) -> None:
    """解析不出标题的文件名入库（纯季集/编号等），供管理员跟进解析器。"""
    if name and name_parser.parse(name).titleless:
        db.record_name_issue(config.DB_PATH, name, source)


def _spec_badges(name: str) -> list[str]:
    """从文件名提取规格标注（分辨率/来源/编码/音频/附加/字幕/语言），去重保序。

    由 MessyNameParser 统一解析（4K→2160p 归一化、HDR/DV/10bit 附加标签、
    杜比全景声等中文规格词均可识别）。"""
    p = name_parser.parse(name)
    out: list[str] = []
    for v in (p.resolution, p.source, p.codec, *p.audio, *p.extras,
              *p.subtitle, *p.languages):
        if v and v not in out:
            out.append(v)
    return out


def _spec_merge(*names: str) -> dict:
    """合并多个文件名的规格（spec_dict）；标量维度前者优先，列表维度并集。

    AI 规范名通常带 分辨率/来源/编码，原名常带 字幕/音轨信息（如 中英双字），
    合并后徽标信息最全。"""
    spec: dict = {}
    for nm in names:
        if not nm:
            continue
        for k, v in spec_dict(nm).items():
            if isinstance(v, list):
                cur = spec.setdefault(k, [])
                for x in v:
                    if x not in cur:
                        cur.append(x)
            elif v and not spec.get(k):
                spec[k] = v
    return spec


def _task_files(task: dict) -> list[dict]:
    """COMPLETED 任务的落盘文件明细（需求第 10 条）：

    - name：原名（分享内文件名）；final：重命名后的规范名（未改名=原名）；
    - renamed/canonical_source：智能重命名标记与来源（ai=AI 规范名）；
    - season/episode/spec：从最终名+原名合并解析（SxxEyy 徽标与规格展示）；
    - exists：转存前网盘已有标记（worker 防重检测结果）。"""
    try:
        result = json.loads(task.get("result_json") or "{}")
    except ValueError:
        result = {}
    existed = set(result.get("existed") or [])  # 转存前网盘已有（提示不拦截）
    # 智能重命名（需求第 8 条）：renamed 为实际改名成功的对照，rename_map 为计划
    renamed = {a: b for a, b in (result.get("renamed") or [])}
    plan = {os.path.basename(k): v
            for k, v in (result.get("rename_map") or {}).items()}
    raw: list[dict] = []
    if result.get("name"):  # 单文件定位（targeted）
        raw = [{"name": result.get("origin_name") or result["name"],
                "size": result.get("size") or 0}]
    elif result.get("files"):  # 整包/多选落盘清单
        raw = list(result["files"])
    elif result.get("names"):  # 老任务仅有路径列表（无 size）
        raw = [{"name": n, "size": 0} for n in result["names"]]
    if result.get("mode") == "picks" and result.get("names"):
        # 历史任务兜底：老版 worker 落盘清单用网盘模糊搜索补全，混入同目录
        # 其他任务的文件；picks 按本次实际所选文件名过滤（显示层修复）
        want = {os.path.basename(n) for n in result["names"]}
        raw = [f for f in raw
               if os.path.basename(f.get("origin") or f["name"]) in want]
    for f in raw:
        origin = f.get("origin") or f["name"]  # worker 改名后 origin 存原名
        final = renamed.get(origin) or plan.get(origin) or origin
        f["name"] = origin
        f["final"] = final
        f["renamed"] = final != origin
        f["canonical_source"] = "ai" if (f["renamed"] and result.get("ai_renamed")) \
            else ("parser" if f["renamed"] else "")
        info = name_parser.parse(final)
        f["season"], f["episode"] = info.season, info.episode
        f["size_h"] = _human_size(f.get("size") or 0)
        f["spec"] = _spec_badges(final)
        for v in _spec_badges(origin):  # 原名里的字幕/音轨等规格并进去
            if v not in f["spec"]:
                f["spec"].append(v)
        if f["renamed"]:
            f["canonical"] = final  # 已按规范名落盘
        else:  # 未改名：展示规范名建议
            f["canonical"] = canonical_with_context(
                origin, search_title=task.get("title") or "",
                share_title=task.get("candidate_title") or "")
        f["exists"] = origin in existed or \
            os.path.basename(origin) in existed
        _record_unparsed(origin, "task")
    return raw


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


@router.get("/api/task/{task_id}")
async def task_api(request: Request, task_id: str):
    user, err = require_user(request)
    if err:
        return err
    task = db.get_task(config.DB_PATH, task_id)
    if task is None:
        return JSONResponse({"detail": "任务不存在"}, status_code=404)
    # 归属校验（防遍历）：仅本人与管理员可见
    if user["role"] != "admin" and task["user_id"] != user["id"]:
        return JSONResponse({"detail": "任务不存在"}, status_code=404)
    task["status_text"] = _STATUS_TEXT.get(task["status"], task["status"])
    task["save_dir"] = settings.get("TRANSFER_DIR")  # 落盘目录名（任务页展示用）
    if task["status"] == "COMPLETED":
        task["files"] = _task_files(task)  # 落盘文件明细（含规格与命名提示）
        try:  # 智能重命名摘要（需求第 8 条：任务页展示 + 记录文件名）
            result = json.loads(task.get("result_json") or "{}")
        except ValueError:
            result = {}
        if result.get("renamed"):
            task["renamed_count"] = len(result["renamed"])
        if result.get("rename_map") or result.get("renamed"):
            task["rename_note"] = f"已有{settings.get('APP_NAME')}智能重命名.txt"
    return task
