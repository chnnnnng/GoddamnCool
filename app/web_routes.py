"""Web 授权页（前后端分离）：页面为 web/ 下静态文件，接口仅返回 JSON。

- GET /setup 返回 web/setup.html（FileResponse，no-store）；
- POST /setup 接收表单字段（token/cookie），返回 {ok, message} JSON，
  由前端 web/js/setup.js 展示结果。
"""
from fastapi import APIRouter, Form
from fastapi.responses import FileResponse, JSONResponse

from . import config, db
from .providers.quark import QuarkDriveProvider
from .providers.base import ProviderError
from .stream_service import StreamService

router = APIRouter()
service = StreamService()


def page(name: str) -> FileResponse:
    """返回前端静态页（候选页/任务页路由也复用）。管理页禁止缓存。"""
    return FileResponse(config.WEB_DIR / name, headers={"Cache-Control": "no-store"})


@router.get("/setup", response_class=FileResponse)
async def setup_page():
    return page("setup.html")


@router.post("/setup")
async def setup_submit(token: str = Form(...), cookie: str = Form(...)):
    if token != config.ADMIN_TOKEN:
        return JSONResponse({"ok": False, "message": "访问令牌错误"}, status_code=403)
    cookie = cookie.strip().strip('"').strip("'")
    if "__puus" not in cookie:
        return JSONResponse({"ok": False,
                             "message": "Cookie 不完整（缺少 __puus 字段），请重新完整复制"},
                            status_code=400)
    provider = QuarkDriveProvider(cookie=cookie, db_path=config.DB_PATH)
    try:
        await provider.verify()
    except ProviderError as e:
        return JSONResponse({"ok": False, "message": f"Cookie 验证失败：{e}"}, status_code=400)
    finally:
        await provider.close()
    db.save_cookie(config.DB_PATH, cookie)
    return JSONResponse({"ok": True, "message": "绑定成功！请在 Stremio 中安装/刷新 Add-on"})


@router.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "version": config.ADDON_VERSION,
        "cookie_bound": service.load_cookie() is not None,
    })
