"""注册 / 登录 / 会话 / 用户身份 API 与页面路由。

注册规则：
- 必须携带邀请码（invite == ADMIN_TOKEN）；未配置 ADMIN_TOKEN 时注册关闭；
- 首个注册用户自动成为管理员（role=admin, tier=pro），其后均为普通用户；
- 暂不做邮箱验证，仅要求格式含 @ 且密码 >= 8 位。

会话为纯 header token（X-Session-Token / Authorization: Bearer），由前端保存在
localStorage，不进 URL、不落日志。
"""
import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config, db, settings
from .auth import (SESSION_TTL, hash_password, new_session_token,
                   new_stream_token, require_user, verify_password)
from .web_routes import page

router = APIRouter()


def _validate_email(email: str) -> str | None:
    email = email.strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return None
    return email


@router.post("/api/auth/register")
async def register(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    email = _validate_email(body.get("email") or "")
    password = body.get("password") or ""
    invite = body.get("invite") or ""
    if not email:
        return JSONResponse({"detail": "邮箱格式无效"}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"detail": "密码至少 8 位"}, status_code=400)
    if not config.ADMIN_TOKEN or not hmac.compare_digest(invite, config.ADMIN_TOKEN):
        return JSONResponse({"detail": "邀请码错误"}, status_code=403)
    if db.get_user_by_email(config.DB_PATH, email):
        return JSONResponse({"detail": "该邮箱已注册"}, status_code=409)
    first = db.count_users(config.DB_PATH) == 0
    role = "admin" if first else "user"
    tier = "pro" if first else "lite"
    user = db.create_user(config.DB_PATH, email, hash_password(password),
                          role, tier, new_stream_token())
    token = new_session_token()
    db.create_session(config.DB_PATH, token, user["id"], SESSION_TTL)
    return {"token": token,
            "user": {"id": user["id"], "email": user["email"],
                     "role": role, "tier": tier}}


@router.post("/api/auth/login")
async def login(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    email = (body.get("email") or "").strip().lower()
    user = db.get_user_by_email(config.DB_PATH, email)
    if not user or not verify_password(body.get("password") or "",
                                       user["password_hash"]):
        return JSONResponse({"detail": "邮箱或密码错误"}, status_code=401)
    if user["disabled"]:
        return JSONResponse({"detail": "账号已被禁用"}, status_code=403)
    token = new_session_token()
    db.create_session(config.DB_PATH, token, user["id"], SESSION_TTL)
    return {"token": token,
            "user": {"id": user["id"], "email": user["email"],
                     "role": user["role"], "tier": user["tier"]}}


@router.post("/api/auth/logout")
async def logout(request: Request):
    token = (request.headers.get("X-Session-Token", "")
             or (request.headers.get("Authorization", "")[7:].strip()
                 if request.headers.get("Authorization", "").lower().startswith("bearer ")
                 else "")).strip()
    if token:
        db.delete_session(config.DB_PATH, token)
    return {"ok": True}


def _quota_payload(user: dict) -> dict:
    """两级配额回显（搜索/转存）：今日用量 + 限额/不限 + 历史累计。"""
    unlimited = user["role"] == "admin" or user["tier"] == "pro"
    return {
        "search": {
            "used": db.get_search_count(config.DB_PATH, user["id"]),
            "limit": settings.get("LITE_DAILY_SEARCH_LIMIT"),
            "unlimited": unlimited,
            "total": db.get_search_total(config.DB_PATH, user["id"]),
        },
        "transfer": {
            "used": db.get_transfer_count(config.DB_PATH, user["id"]),
            "limit": settings.get("LITE_DAILY_TRANSFER_LIMIT"),
            "unlimited": unlimited,
            "total": db.get_transfer_total(config.DB_PATH, user["id"]),
        },
        # 播放无配额限制，仅统计展示
        "play": {
            "used": db.get_play_count(config.DB_PATH, user["id"]),
            "total": db.get_play_total(config.DB_PATH, user["id"]),
        },
    }


@router.get("/api/auth/me")
async def me(request: Request):
    user, err = require_user(request)
    if err:
        return err
    cookie_bound = db.load_user_cookie(config.DB_PATH, user["id"]) is not None
    return {
        "email": user["email"], "role": user["role"], "tier": user["tier"],
        "created_at": user["created_at"],
        "save_dir": user.get("save_dir") or "",
        # 服务器默认保存目录（管理员在 /admin 配置；空表示网盘根目录）
        "default_save_dir": settings.get("TRANSFER_DIR") or "",
        "cookie_bound": cookie_bound,
        "quota": _quota_payload(user),
        "addon_url": f"{config.BASE_URL}/s/{user['stream_token']}/manifest.json",
    }


@router.get("/")
async def landing_page():
    """产品首页：未登录展示介绍，已登录由前端 JS 跳转 /panel。"""
    return page("landing.html")


@router.get("/login")
async def login_page():
    return page("login.html")


@router.get("/panel")
async def panel_page():
    return page("panel.html")


@router.get("/admin")
async def admin_page():
    return page("admin.html")
