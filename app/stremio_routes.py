"""Stremio 协议端点：/manifest.json 与 /stream/{type}/{id}.json。"""
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import config
from .stream_service import StreamService

router = APIRouter()
service = StreamService()


@router.get("/manifest.json")
async def manifest():
    return {
        "id": config.ADDON_ID,
        "version": config.ADDON_VERSION,
        "name": config.APP_NAME,  # Add-on 显示名与页面标题统一（.env APP_NAME 可改）
        "description": config.ADDON_DESCRIPTION,
        "contactEmail": config.ADDON_CONTACT_EMAIL,
        "resources": ["stream"],
        "types": ["movie", "series"],
        "catalogs": [],
        "idPrefixes": ["tt"],
    }


@router.get("/stream/{vtype}/{video_id}.json")
async def stream(vtype: str, video_id: str):
    streams, status = await service.get_streams(vtype, video_id)
    return JSONResponse(
        {"streams": streams},
        headers={"X-Addon-Status": quote(status or "ok", safe="")},
    )
