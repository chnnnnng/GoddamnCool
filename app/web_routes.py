"""健康检查与前端页面渲染工具（页面路由由各功能模块复用 page()）。"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from . import config

router = APIRouter()


def page(name: str) -> Response:
    """返回前端静态页（候选页/任务页路由也复用）。管理页禁止缓存；
    页面内的 {{APP_NAME}} 占位符在返回前替换为配置的产品名称。"""
    html = (config.WEB_DIR / name).read_text(encoding="utf-8")
    return Response(
        html.replace("{{APP_NAME}}", config.APP_NAME),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/health")
async def health():
    return JSONResponse({"status": "ok", "version": config.ADDON_VERSION})
