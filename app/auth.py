"""认证与鉴权：密码哈希、会话 token、请求身份解析。

身份来源（二选一）：
1. X-Session-Token / Authorization: Bearer：注册用户会话；
2. 未认证。
"""
import hashlib
import hmac
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from . import config, db

# 会话有效期（秒）：30 天
SESSION_TTL = 30 * 86400
# PBKDF2 迭代次数（OWASP 2023 推荐量级）
PBKDF2_ITER = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER)
    return f"pbkdf2${PBKDF2_ITER}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                     bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_stream_token() -> str:
    return secrets.token_hex(16)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _session_token(request: Request) -> str:
    return (request.headers.get("X-Session-Token", "")
            or _bearer(request)).strip()


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def resolve_user(request: Request) -> dict | None:
    """解析请求身份：会话 token → 注册用户；否则 None。"""
    token = _session_token(request)
    if not token:
        return None
    return db.get_session_user(config.DB_PATH, token)


def require_user(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """要求已登录。失败返回 (None, 401)。"""
    user = resolve_user(request)
    if user is None:
        return None, JSONResponse({"detail": "请先登录"}, status_code=401)
    return user, None


def require_admin(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """要求管理员角色。先 401（未登录）后 403（非管理员）。"""
    user, err = require_user(request)
    if err:
        return None, err
    if user["role"] != "admin":
        return None, JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    return user, None
