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
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config, db, settings
from .auth import require_user
from .matcher import VIDEO_EXTS, parse, parse_file_spec
from .name_parser import canonical_with_context
from .name_parser import parser as name_parser
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


# 网盘文件名集合缓存 {user_id: (拉取时间, 文件名集合)}；TTL 60s——
# 转存/改名后最多延迟 1 分钟反映到"网盘中已有 N 项"，避免每次搜索都全盘递归
_DRIVE_EXISTING_TTL = 60.0
_drive_existing_cache: dict[str, tuple[float, set[str]]] = {}


async def _drive_existing_names(cookie: str, user_id: str) -> set[str]:
    """拉取用户网盘全部文件名集合（list_all_file_names 递归列取），失败返回空集合。"""
    if not cookie:
        return set()
    now = time.monotonic()
    hit = _drive_existing_cache.get(user_id)
    if hit and now - hit[0] < _DRIVE_EXISTING_TTL:
        return hit[1]
    provider = QuarkDriveProvider(cookie=cookie, db_path=config.DB_PATH,
                                  user_id=user_id)
    try:
        names = await provider.list_all_file_names()
    except Exception:  # 网盘列名失败不阻塞搜索，仅无提示
        return set()
    finally:
        await provider.close()
    _drive_existing_cache[user_id] = (now, names)
    return names


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
    meta, cands, errors, keyword = await candidate_service.search(
        vtype, id, keyword=kw, disks=disk_list, cookie=user_cookie)
    # "网盘中已有 N 项"实时补算：不依赖搜索缓存内容（detail 可能来自 1 天前
    # 的缓存），缓存命中与否两条路径都覆盖。计数进 existing_count（卡片 badge
    # 直显），逐文件 exists 标志供"查看内容"弹窗直接渲染绿勾——有效候选的
    # detail 已含完整文件列表，点开弹窗无需再解析分享
    existing = await _drive_existing_names(user_cookie, user["id"])
    for c in cands:
        d = getattr(c, "detail", None)
        if not (d and d.get("files")):
            continue
        for f in d["files"]:
            nm = f.get("name") or ""
            if existing:
                f["exists"] = nm in existing \
                    or os.path.basename(nm) in existing
            # 缓存中的旧 detail 无 warn：补算命名规范提示（与任务页同一规则；
            # 搜索场景无季集目标，仅查无标题）
            f.setdefault("warn", _name_warn(nm, None, None))
            # 规范文件名：无标题名结合搜索名/分享名/文件夹名兜底
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
    # 文件名不规范标志（与任务页同一规则，inspect 无季集目标，仅查无标题）
    ctx_kw = (body.get("kw") or "").strip()
    ctx_title = (body.get("title") or "").strip()[:300]  # 候选/分享标题（前端传）
    for f in data.get("files", []):
        nm = f.get("name") or ""
        f["warn"] = _name_warn(nm, None, None)
        # 规范文件名：无标题名结合搜索名/分享名/文件夹名兜底
        f["canonical"] = canonical_with_context(
            nm, search_title=ctx_kw, share_title=ctx_title,
            folder=_folder_of(f.get("path") or ""))
        _record_unparsed(nm, "search")
    # "网盘中已存在"标记：实时比对用户网盘文件名集合（不随 inspect 缓存过期）
    data["existing_count"] = 0
    try:
        provider = QuarkDriveProvider(cookie=cookie, db_path=config.DB_PATH,
                                      user_id=user["id"])
        try:
            existing = await provider.list_all_file_names()
        finally:
            await provider.close()
        for f in data.get("files", []):
            nm = f.get("name") or ""
            f["exists"] = nm in existing or os.path.basename(nm) in existing
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
    """从文件名提取规格标注（分辨率/来源/编码/音频/字幕/语言），去重保序。"""
    spec = parse_file_spec(name)
    out: list[str] = []
    for v in (spec.get("resolution"), spec.get("source"), spec.get("codec"),
              *spec.get("audio", []), *spec.get("subtitle", []),
              *spec.get("languages", [])):
        if v and v not in out:
            out.append(v)
    return out


# 仅剩季集/集数 token 的"无标题"文件名（如 SE03.11 / 06 / EP06 / 第11集）——
# 与 matcher 一样无法凭标题定位，换源或分享重发后会匹配失败
_TITLELESS_TOKEN = re.compile(
    r"(?i)^(?:s\d{1,2}e\d{1,3}|se\d{1,2}(?:[.\-_ ]?\d{1,3})?|\d{1,3}x\d{1,3}|"
    r"ep?\d{1,3}|s\d{1,2}|e\d{1,3}|\d{1,3}|"
    r"第\s*\d{1,3}\s*[集话期]|第\s*[0-9一二三四五六七八九十百]+\s*季)$")


def _is_meta_token(t: str) -> bool:
    """token 是否为季集/集数标注（无标题判定时剥除）。
    4 位纯数字仅在年份区间（1900-2099）外视为集数（如长篇 1001 集），
    区间内保留为可能的片名（如《2012》《1917》）。"""
    if _TITLELESS_TOKEN.match(t):
        return True
    return t.isdigit() and len(t) == 4 and not (1900 <= int(t) <= 2099)


def _name_warn(name: str, season: int | None, episode: int | None) -> str:
    """文件名疑似不符合匹配规范时的修改提示（命名应含标题与季集信息）。
    规则与 Stream 侧 matcher 一致：仅视频文件参与检查；剥除季集/集数标注后
    无剩余文字视为无标题；剧集任务解析不出集数（含裸集数文件名）同样提示。"""
    if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
        return ""  # 非视频文件（nfo/海报等）不参与命名规范检查
    p = parse(name)
    stem = re.sub(r"^\[.*?\]", "", os.path.splitext(name)[0])
    if not [t for t in re.split(r"[\s._\-\[\]()]+", stem)
            if t and not _is_meta_token(t)]:
        return "未识别出标题，建议改为「影片名 S01E02」等规范命名"
    if episode is not None and p.episode is None:
        return "未识别出集数，建议在文件名中补充 SxxEyy 标注"
    if season is not None and p.season is not None and p.season != season:
        return f"文件名标注为第 {p.season} 季，与任务目标第 {season} 季不符"
    return ""


def _task_files(task: dict) -> list[dict]:
    """COMPLETED 任务的落盘文件明细：文件名/大小/规格/命名规范提示/网盘已有标记。"""
    try:
        result = json.loads(task.get("result_json") or "{}")
    except ValueError:
        result = {}
    existed = set(result.get("existed") or [])  # 转存前网盘已有同名（提示不拦截）
    raw: list[dict] = []
    if result.get("name"):  # 单文件定位（targeted）
        raw = [{"name": result["name"], "size": result.get("size") or 0}]
    elif result.get("files"):  # 整包/多选落盘清单
        raw = list(result["files"])
    elif result.get("names"):  # 老任务仅有路径列表（无 size）
        raw = [{"name": n, "size": 0} for n in result["names"]]
    if result.get("mode") == "picks" and result.get("names"):
        # 历史任务兜底：老版 worker 落盘清单用网盘模糊搜索补全，混入同目录
        # 其他任务的文件；picks 按本次实际所选文件名过滤（显示层修复）
        want = {os.path.basename(n) for n in result["names"]}
        raw = [f for f in raw if os.path.basename(f["name"]) in want]
    for f in raw:
        f["size_h"] = _human_size(f.get("size") or 0)
        f["spec"] = _spec_badges(f["name"])
        f["warn"] = _name_warn(f["name"], task.get("season"),
                               task.get("episode"))
        f["name_ok"] = not f["warn"]
        # 规范文件名：任务锚定影片标题（title）与分享资源标题兜底
        f["canonical"] = canonical_with_context(
            f["name"], search_title=task.get("title") or "",
            share_title=task.get("candidate_title") or "")
        f["exists"] = f["name"] in existed or \
            os.path.basename(f["name"]) in existed
        _record_unparsed(f["name"], "task")
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
    return task
