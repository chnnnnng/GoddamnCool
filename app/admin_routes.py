"""管理员 API：用户管理（建号/改级别/禁用/删除）与运行时配置修改。

自我保护：管理员不能禁用自己、不能修改自己的角色、不能删除自己，
避免误操作导致系统失去管理入口。
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config, db, settings
from .auth import hash_password, new_stream_token, require_admin

router = APIRouter()


@router.get("/api/admin/users")
async def list_users(request: Request):
    _, err = require_admin(request)
    if err:
        return err
    counts = db.count_tasks_grouped(config.DB_PATH)
    users = []
    for u in db.list_users(config.DB_PATH):
        users.append({
            "id": u["id"], "email": u["email"], "role": u["role"],
            "tier": u["tier"], "disabled": bool(u["disabled"]),
            "created_at": u["created_at"], "stream_token": u["stream_token"],
            "save_dir": u.get("save_dir") or "",
            "quota_used": db.get_search_count(config.DB_PATH, u["id"]),
            "transfer_used": db.get_transfer_count(config.DB_PATH, u["id"]),
            "task_count": counts.get(u["id"], 0),
            "cookie_bound": db.load_user_cookie(config.DB_PATH, u["id"]) is not None,
        })
    return {"users": users}


@router.get("/api/admin/name-issues")
async def list_name_issues(request: Request):
    """解析问题文件名清单（无法解析出标题的文件名，供跟进名称解析器）。"""
    _, err = require_admin(request)
    if err:
        return err
    return {"issues": db.list_name_issues(config.DB_PATH)}


@router.post("/api/admin/users")
async def create_user(request: Request):
    _, err = require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    tier = body.get("tier") or "lite"
    role = body.get("role") or "user"
    if not email or "@" not in email:
        return JSONResponse({"detail": "邮箱格式无效"}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"detail": "密码至少 8 位"}, status_code=400)
    if tier not in ("lite", "pro"):
        return JSONResponse({"detail": "tier 必须是 lite 或 pro"}, status_code=400)
    if role not in ("user", "admin"):
        return JSONResponse({"detail": "role 必须是 user 或 admin"}, status_code=400)
    if db.get_user_by_email(config.DB_PATH, email):
        return JSONResponse({"detail": "该邮箱已注册"}, status_code=409)
    user = db.create_user(config.DB_PATH, email, hash_password(password),
                          role, tier, new_stream_token())
    return {"ok": True, "id": user["id"]}


@router.post("/api/admin/users/{user_id}")
async def modify_user(request: Request, user_id: str):
    admin, err = require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    if not db.get_user(config.DB_PATH, user_id):
        return JSONResponse({"detail": "用户不存在"}, status_code=404)
    updates: dict = {}
    if "tier" in body:
        if body["tier"] not in ("lite", "pro"):
            return JSONResponse({"detail": "tier 必须是 lite 或 pro"}, status_code=400)
        updates["tier"] = body["tier"]
    if "role" in body:
        if body["role"] not in ("user", "admin"):
            return JSONResponse({"detail": "role 必须是 user 或 admin"}, status_code=400)
        if user_id == admin["id"] and body["role"] != "admin":
            return JSONResponse({"detail": "不能降低自己的管理员角色"}, status_code=400)
        updates["role"] = body["role"]
    if "disabled" in body:
        if not isinstance(body["disabled"], bool):
            return JSONResponse({"detail": "disabled 必须是布尔值"}, status_code=400)
        if user_id == admin["id"] and body["disabled"]:
            return JSONResponse({"detail": "不能禁用自己"}, status_code=400)
        updates["disabled"] = 1 if body["disabled"] else 0
    if "password" in body:
        if len(body["password"] or "") < 8:
            return JSONResponse({"detail": "密码至少 8 位"}, status_code=400)
        updates["password_hash"] = hash_password(body["password"])
    if not updates:
        return JSONResponse({"detail": "没有需要修改的字段"}, status_code=400)
    db.update_user(config.DB_PATH, user_id, **updates)
    if updates.get("disabled"):
        db.delete_user_sessions(config.DB_PATH, user_id)  # 禁用即踢下线
    return {"ok": True}


@router.delete("/api/admin/users/{user_id}")
async def remove_user(request: Request, user_id: str):
    admin, err = require_admin(request)
    if err:
        return err
    if user_id == admin["id"]:
        return JSONResponse({"detail": "不能删除自己"}, status_code=400)
    if not db.delete_user(config.DB_PATH, user_id):
        return JSONResponse({"detail": "用户不存在"}, status_code=404)
    return {"ok": True}


@router.get("/api/admin/config")
async def get_config(request: Request):
    _, err = require_admin(request)
    if err:
        return err
    return {"config": [
        {"key": key, "value": settings.get(key),
         "meta": settings.SETTINGS_META.get(key, "")}
        for key in settings.EDITABLE
    ]}


@router.post("/api/admin/config")
async def set_config(request: Request):
    _, err = require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "请求体必须是 JSON 对象"}, status_code=400)
    try:
        config_data = settings.set(config.DB_PATH, body)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    return {"ok": True, "config": config_data}
