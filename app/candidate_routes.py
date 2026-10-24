"""候选资源搜索与转存任务（阶段 3，前后端分离）。

页面为 web/ 下静态文件（candidates.html / task.html），本模块只提供
JSON API 与页面路由；所有 /api/* 接口均要求 X-Admin-Token 头等于
ADMIN_TOKEN（需求第 10 条），token 由前端保存于 localStorage，
不进 URL、不落日志。

安全要点：
- 第三方候选标题在前端一律 textContent 渲染，防存储型 XSS；
- 确认转存时服务端校验分享链接格式（仅夸克官方分享域，防 SSRF/任意 URL）；
- 任务 ID 为 uuid4 hex，防猜测遍历；影片标题以服务端 TMDB 结果为准。
"""
import hmac

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

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


def _check_token(request: Request) -> bool:
    token = request.headers.get("X-Admin-Token", "")
    return bool(config.ADMIN_TOKEN) and hmac.compare_digest(token, config.ADMIN_TOKEN)


@router.get("/candidates", response_class=FileResponse)
async def candidates_page():
    return page("candidates.html")


@router.get("/api/candidates/search")
async def candidates_search(request: Request, vtype: str = "movie", id: str = "",
                            kw: str = ""):
    if not _check_token(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
    if not id:
        return JSONResponse({"detail": "缺少影片 ID"}, status_code=400)
    meta, cands, errors, keyword = await candidate_service.search(vtype, id, keyword=kw)
    if meta is None:
        return JSONResponse({"detail": errors or "影片元数据获取失败"}, status_code=404)
    return {
        "meta": {"id": meta.id, "title": meta.title, "year": meta.year, "type": meta.type},
        "keyword": keyword,
        "candidates": [{
            "title": c.title, "share_url": c.share_url, "passcode": c.passcode,
            "source": c.source, "published": c.published,
            "provider": c.provider, "score": c.score,
            "alive_state": c.alive_state, "alive_summary": c.alive_summary,
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
    if vtype not in ("movie", "series") or not video_id:
        return JSONResponse({"detail": "vtype/id 参数无效"}, status_code=400)
    # 分享链接服务端强校验：仅接受夸克官方分享域（需求第 10 条）
    if not SHARE_URL_RE.match(share_url):
        return JSONResponse({"detail": "分享链接无效（仅支持 pan.quark.cn 分享链接）"},
                            status_code=400)
    imdb_id, season, episode = parse_video_id(video_id)
    meta = await fetch_meta(imdb_id, vtype, candidate_service.db_path)
    if meta is None or not meta.title:
        return JSONResponse({"detail": "影片元数据获取失败，请稍后重试"}, status_code=400)
    mode = body.get("mode") or "episode"
    if mode not in ("episode", "whole"):
        return JSONResponse({"detail": "mode 参数无效（episode/whole）"}, status_code=400)
    # whole（转存完整剧集）：任务不锚定季集，整包落盘；episode 仅转存所选集
    task, created = db.create_task(
        config.DB_PATH, vtype=vtype, imdb_id=imdb_id,
        season=None if mode == "whole" else season,
        episode=None if mode == "whole" else episode,
        title=meta.title,
        candidate_title=(body.get("title") or "")[:300],
        share_url=share_url,
        passcode=(body.get("passcode") or "")[:32],
        source=(body.get("source") or "")[:100],
        mode=mode,
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


@router.get("/task/{task_id}", response_class=FileResponse)
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
    return task
