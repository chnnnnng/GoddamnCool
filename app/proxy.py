"""受控代理流：服务器带头向夸克取流并转发，解决客户端直连 CDN 节点绕网慢的问题。

URL 形如 /proxy/{fid}?e={过期时间}&s={签名}，签名 = HMAC-SHA256(SECRET_KEY, fid:e)，
防止端点被滥用为公开代理。
"""
import hashlib
import hmac
import logging
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from . import config, db
from .providers.base import CloudFile
from .providers.quark import REFERER, USER_AGENT, QuarkDriveProvider

log = logging.getLogger(__name__)
router = APIRouter()

_CHUNK = 256 * 1024
TOKEN_TTL = 12 * 3600  # 与夸克直链签名有效期同级


def _sign(fid: str, exp: int) -> str:
    msg = f"{fid}:{exp}".encode()
    return hmac.new(config.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:32]


def make_proxy_url(fid: str) -> str:
    exp = int(time.time()) + TOKEN_TTL
    return f"{config.BASE_URL}/proxy/{fid}?e={exp}&s={_sign(fid, exp)}"


def verify_proxy_url(fid: str, exp: str, sig: str) -> bool:
    try:
        if int(exp) < time.time():
            return False
    except ValueError:
        return False
    return hmac.compare_digest(_sign(fid, int(exp)), sig)


def _build_client(cookie: str) -> httpx.AsyncClient:
    # 注意：CDN 直链的 auth_key 签名覆盖完整 URI（含查询串），
    # 不能像 API 端点那样追加 pr/fr 参数，否则 CDN 返回 403
    return httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=10, read=30),
        follow_redirects=True,
        headers={"Cookie": cookie, "Referer": REFERER, "User-Agent": USER_AGENT},
    )


async def _upstream_url(fid: str) -> tuple[str, dict]:
    cookie = db.load_cookie(config.DB_PATH) or config.QUARK_COOKIE
    if not cookie:
        raise RuntimeError("未绑定网盘账号")
    p = QuarkDriveProvider(cookie=cookie, db_path=config.DB_PATH)
    try:
        res = await p.get_play_resource(CloudFile(fid=fid, name="", size=0, is_dir=False))
        return res.url, res.headers
    finally:
        await p.close()


@router.get("/proxy/{fid}")
async def proxy(fid: str, e: str = "", s: str = "", request: Request = None):
    if not verify_proxy_url(fid, e, s):
        return StreamingResponse(iter(["forbidden"]), status_code=403)
    try:
        url, headers = await _upstream_url(fid)
    except Exception as ex:
        log.error("代理取直链失败: %s", ex)
        return StreamingResponse(iter(["upstream error"]), status_code=502)

    fwd = {}
    rng = request.headers.get("range") if request else None
    if rng:
        fwd["Range"] = rng
    client = _build_client(headers["Cookie"])
    req = client.build_request("GET", url, headers=fwd)
    try:
        resp = await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        raise

    passthrough = ("content-type", "content-length", "content-range",
                   "accept-ranges", "last-modified", "etag")
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() in passthrough}

    async def stream():
        try:
            async for chunk in resp.aiter_bytes(_CHUNK):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(stream(), status_code=resp.status_code,
                             headers=out_headers, media_type=resp.headers.get("content-type"))
