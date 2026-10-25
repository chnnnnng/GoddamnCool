"""用户面板 API：绑定个人网盘 Cookie、状态检测、自定义保存目录、改密码、
轮换 Add-on 令牌、我的任务。

所有接口要求登录（X-Session-Token）。

Cookie 为敏感数据：经 Fernet 加密落库 user_cookies 表，接口永不回显明文，
仅返回绑定状态。TMDB key 由管理员统一配置，普通用户不可见。
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config, db
from .auth import hash_password, new_stream_token, require_user, verify_password
from .providers.base import ProviderError
from .providers.quark import QuarkDriveProvider
from .candidate_routes import _STATUS_TEXT

router = APIRouter()


def _real_user(request: Request) -> tuple[dict | None, JSONResponse | None]:
    return require_user(request)


def _clean_cookie(raw: str) -> str:
    return (raw or "").strip().strip('"').strip("'")


@router.post("/api/me/cookie")
async def bind_cookie(request: Request):
    user, err = _real_user(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    cookie = _clean_cookie(body.get("cookie") or "")
    if "__puus" not in cookie:
        return JSONResponse(
            {"detail": "Cookie 不完整（缺少 __puus 字段），请重新完整复制"},
            status_code=400)
    provider = QuarkDriveProvider(cookie=cookie, db_path=config.DB_PATH)
    try:
        await provider.verify()
    except ProviderError as e:
        return JSONResponse({"detail": f"Cookie 验证失败：{e}"}, status_code=400)
    finally:
        await provider.close()
    db.save_user_cookie(config.DB_PATH, user["id"], cookie)
    return {"ok": True, "message": "网盘 Cookie 绑定成功"}


@router.delete("/api/me/cookie")
async def unbind_cookie(request: Request):
    user, err = _real_user(request)
    if err:
        return err
    db.delete_user_cookie(config.DB_PATH, user["id"])
    return {"ok": True, "message": "已解除网盘 Cookie 绑定"}


@router.post("/api/me/cookie/check")
async def check_cookie(request: Request):
    """检测已绑定的网盘 Cookie 是否仍然有效（实时调用夸克验证接口）。"""
    user, err = _real_user(request)
    if err:
        return err
    cookie = db.load_user_cookie(config.DB_PATH, user["id"])
    if not cookie:
        return JSONResponse({"detail": "尚未绑定网盘 Cookie"}, status_code=400)
    provider = QuarkDriveProvider(cookie=cookie, db_path=config.DB_PATH,
                                  user_id=user["id"])
    try:
        await provider.verify()
    except ProviderError as e:
        return {"ok": False, "message": f"Cookie 已失效：{e}，请重新绑定"}
    finally:
        await provider.close()
    return {"ok": True, "message": "Cookie 有效，网盘登录态正常"}


@router.post("/api/me/save_dir")
async def set_save_dir(request: Request):
    """自定义转存保存目录（自己网盘根目录下的文件夹名，留空用服务器默认）。"""
    user, err = _real_user(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    save_dir = (body.get("save_dir") or "").strip()[:64]
    db.update_user(config.DB_PATH, user["id"], save_dir=save_dir)
    msg = (f"保存目录已设为「{save_dir}」" if save_dir
           else "已恢复使用服务器默认保存目录")
    return {"ok": True, "save_dir": save_dir, "message": msg}


@router.delete("/api/me/save_dir")
async def clear_save_dir(request: Request):
    user, err = _real_user(request)
    if err:
        return err
    db.update_user(config.DB_PATH, user["id"], save_dir="")
    return {"ok": True, "save_dir": "", "message": "已恢复使用服务器默认保存目录"}


@router.post("/api/me/password")
async def change_password(request: Request):
    user, err = _real_user(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    old = body.get("old_password") or ""
    new = body.get("new_password") or ""
    full = db.get_user(config.DB_PATH, user["id"])
    if not full or not verify_password(old, full["password_hash"]):
        return JSONResponse({"detail": "原密码错误"}, status_code=400)
    if len(new) < 8:
        return JSONResponse({"detail": "新密码至少 8 位"}, status_code=400)
    db.update_user(config.DB_PATH, user["id"], password_hash=hash_password(new))
    return {"ok": True, "message": "密码已修改"}


@router.post("/api/me/token/rotate")
async def rotate_token(request: Request):
    user, err = _real_user(request)
    if err:
        return err
    token = new_stream_token()
    db.update_user(config.DB_PATH, user["id"], stream_token=token)
    return {"ok": True,
            "addon_url": f"{config.BASE_URL}/s/{token}/manifest.json"}


@router.get("/api/me/tasks")
async def my_tasks(request: Request):
    user, err = _real_user(request)
    if err:
        return err
    tasks = db.list_tasks_by_user(config.DB_PATH, user["id"])
    for t in tasks:
        t["status_text"] = _STATUS_TEXT.get(t["status"], t["status"])
        # 附加 TMDB 封面（只读已缓存的元数据，不额外打 API）
        backdrop, poster = None, None
        try:
            imdb_id = t.get("imdb_id")
            vtype = t.get("video_type")
            if imdb_id and vtype:
                cached = db.get_tmdb_meta(config.DB_PATH, imdb_id, vtype,
                                          ttl=config.TMDB_META_TTL)
                if cached:
                    bp = cached.get("backdrop_path")
                    pp = cached.get("poster_path")
                    if bp:
                        backdrop = f"https://image.tmdb.org/t/p/w1280{bp}"
                    if pp:
                        poster = f"https://image.tmdb.org/t/p/w500{pp}"
        except Exception:
            pass
        t["backdrop"] = backdrop
        t["poster"] = poster
    return {"tasks": tasks}
