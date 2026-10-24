"""搜索资源页与转存任务 API（前后端分离）。

页面为 web/ 下静态文件（search.html / task.html），本模块只提供 JSON API
与页面路由；所有 /api/* 接口均要求 X-Admin-Token 头等于 ADMIN_TOKEN
（需求第 10 条），token 由前端保存于 localStorage，不进 URL、不落日志。

两种搜索模式（/search 页面按 URL 参数自动区分）：
- Stremio 锚定模式：/search?vtype=movie&id=tt...（带影片，自动搜 TMDB 双语名，
  转存任务锚定影片，完成后 Stream 自动命中）；
- 独立搜索模式：/search（仅手动关键词 + 网盘类型筛选，转存为 share-only 任务）。

安全要点：
- 第三方候选标题在前端一律 textContent 渲染，防存储型 XSS；
- 确认转存时服务端校验分享链接格式（仅夸克官方分享域，防 SSRF/任意 URL）；
- 任务 ID 为 uuid4 hex，防猜测遍历；影片标题以服务端 TMDB 结果为准。
"""
import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config, db
from .tmdb import fetch_meta
from .providers.search import SHARE_URL_RE
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


def _check_token(request: Request) -> bool:
    token = request.headers.get("X-Admin-Token", "")
    return bool(config.ADMIN_TOKEN) and hmac.compare_digest(token, config.ADMIN_TOKEN)


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
    if not _check_token(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
    try:
        disk_list = _parse_disks(disks)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    if not id and not kw.strip():
        return JSONResponse({"detail": "请输入搜索关键词"}, status_code=400)
    meta, cands, errors, keyword = await candidate_service.search(
        vtype, id, keyword=kw, disks=disk_list)
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
    if not _check_token(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
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
        # Stremio 锚定模式：季集来自影片 ID，标题以 TMDB 为准
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
    task, created = db.create_task(
        config.DB_PATH, vtype=vtype, imdb_id=imdb_id,
        season=None if mode in ("whole", "picks") else season,
        episode=None if mode in ("whole", "picks") else episode,
        title=title,
        candidate_title=(body.get("title") or "")[:300],
        share_url=share_url,
        passcode=(body.get("passcode") or "")[:32],
        source=(body.get("source") or "")[:100],
        mode=mode, picks=picks,
    )
    return {"task_id": task["id"], "created": created, "status": task["status"]}


@router.post("/api/candidates/inspect")
async def candidates_inspect(request: Request):
    """解析分享链接内容清单与文件规格（大小/清晰度/音轨/字幕/季集结构）。

    实时查询夸克分享目录树并解析文件名规格，供候选详情展示与转存选择。"""
    if not _check_token(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    url = (body.get("share_url") or "").strip()
    if not SHARE_URL_RE.match(url):
        return JSONResponse({"detail": "分享链接无效（仅支持 pan.quark.cn 分享链接）"},
                            status_code=400)
    try:
        return await candidate_service.inspect_share(url, (body.get("passcode") or "")[:32])
    except Exception as e:
        return JSONResponse({"detail": f"分享解析失败: {e}"}, status_code=400)


@router.post("/api/candidates/verify")
async def candidates_verify(request: Request):
    """批量检测候选分享链接存活状态（代理 PanSou /api/check/links）。

    前端列出候选后逐个调用本接口标注存活状态；非法链接直接跳过不送检。"""
    if not _check_token(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
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


@router.get("/api/task/{task_id}")
async def task_api(request: Request, task_id: str):
    if not _check_token(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
    task = db.get_task(config.DB_PATH, task_id)
    if task is None:
        return JSONResponse({"detail": "任务不存在"}, status_code=404)
    task["status_text"] = _STATUS_TEXT.get(task["status"], task["status"])
    task["save_dir"] = config.TRANSFER_DIR  # 落盘目录名（任务页展示用）
    return task
