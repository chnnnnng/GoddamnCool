"""Stremio 协议端点：每用户专属 Add-on /s/{stream_token}/...。

注册用户在控制面板获取专属 Add-on 地址，播放/搜索/转存均使用该用户自己
绑定的网盘 Cookie，任务按用户隔离。
"""
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import config, db, settings
from .stream_service import StreamService

router = APIRouter()
service = StreamService()


def _manifest() -> dict:
    # id/描述/图标均可由管理员在线配置（ADDON_ID 变更后 Stremio 视为新 Add-on）
    m = {
        "id": settings.get("ADDON_ID") or config.ADDON_ID,
        "version": config.ADDON_VERSION,
        "name": settings.get("APP_NAME"),
        "description": settings.get("ADDON_DESCRIPTION") or config.ADDON_DESCRIPTION,
        "contactEmail": config.ADDON_CONTACT_EMAIL,
        "resources": ["stream"],
        "types": ["movie", "series"],
        "catalogs": [],
        "idPrefixes": ["tt"],
    }
    icon = settings.get("ADDON_ICON")
    if icon:
        m["logo"] = icon
    return m


def _stream_response(streams: list[dict], status: str) -> JSONResponse:
    return JSONResponse(
        {"streams": streams},
        headers={"X-Addon-Status": quote(status or "ok", safe="")},
    )


@router.get("/s/{stream_token}/manifest.json")
async def user_manifest(stream_token: str):
    user = db.get_user_by_stream_token(config.DB_PATH, stream_token)
    if not user or user["disabled"]:
        return JSONResponse({"detail": "Add-on 地址无效或账号已禁用"},
                            status_code=404)
    return _manifest()


@router.get("/s/{stream_token}/stream/{vtype}/{video_id}.json")
async def user_stream(stream_token: str, vtype: str, video_id: str):
    user = db.get_user_by_stream_token(config.DB_PATH, stream_token)
    if not user:
        return _stream_response([], "Add-on 地址无效")
    if user["disabled"]:
        return _stream_response([], "账号已被禁用，请联系管理员")
    cookie = db.load_user_cookie(config.DB_PATH, user["id"])
    if not cookie:
        # 未绑定 Cookie 时严格隔离：直接给绑定引导
        guide = []
        if config.BASE_URL:
            guide = [{
                "name": f"{config.APP_NAME}\n未绑定网盘",
                "title": "请先绑定你的夸克网盘 Cookie",
                "description": "登录控制面板绑定个人网盘 Cookie 后即可播放",
                "externalUrl": f"{config.BASE_URL}/panel",
            }]
        return _stream_response(guide, "未绑定网盘 Cookie，请在控制面板绑定")
    streams, status = await service.get_streams(
        vtype, video_id, cookie=cookie, user_id=user["id"])
    return _stream_response(streams, status)
