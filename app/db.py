"""SQLite 存储：凭证（Fernet 加密）与转存任务（阶段 3 建记录，阶段 4 执行）。"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

_lock = threading.Lock()
_fernet: Fernet | None = None

# 转存任务状态机（需求第 7 条六态）
TASK_STATUSES = ("PENDING", "SUBMITTING", "PROCESSING", "COMPLETED", "FAILED", "EXPIRED")


def _conn(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def init(db_path: str, secret_key: str) -> None:
    global _fernet
    _fernet = Fernet(secret_key.encode())
    with _lock, _conn(db_path) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS tmdb_cache (
                   imdb_id TEXT NOT NULL,
                   vtype TEXT NOT NULL,
                   data TEXT NOT NULL,            -- JSON：双语名/年份/季集
                   updated_at REAL NOT NULL       -- unix ts，用于 TTL 过期
               , PRIMARY KEY (imdb_id, vtype))"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS kv_cache (
                   key TEXT PRIMARY KEY,          -- 搜索结果/分享详情等缓存键
                   value TEXT NOT NULL,           -- JSON 序列化内容
                   expires_at REAL NOT NULL       -- unix ts，TTL 过期
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS transfer_tasks (
                   id TEXT PRIMARY KEY,               -- uuid4 hex，防猜测遍历
                   video_type TEXT NOT NULL,          -- movie / series
                   imdb_id TEXT NOT NULL,
                   season INTEGER,
                   episode INTEGER,
                   title TEXT NOT NULL,               -- 影片标题（TMDB）
                   candidate_title TEXT NOT NULL,     -- 候选资源标题
                   share_url TEXT NOT NULL,
                   passcode TEXT NOT NULL DEFAULT '',
                   source TEXT NOT NULL DEFAULT '',
                   mode TEXT NOT NULL DEFAULT 'episode',  -- 阶段5：episode 指定集 / whole 整包 / picks 所选多集
                   picks TEXT NOT NULL DEFAULT '',    -- 阶段6：picks 模式所选文件 fid 列表（JSON，参与幂等键）
                   status TEXT NOT NULL DEFAULT 'PENDING',
                   error TEXT NOT NULL DEFAULT '',
                   target_fid TEXT NOT NULL DEFAULT '',   -- 阶段4：转存后文件 fid
                   result_json TEXT NOT NULL DEFAULT '',  -- 阶段4：定位到的文件信息
                   quark_task_id TEXT NOT NULL DEFAULT '', -- 阶段4：夸克侧任务 ID（重启恢复用）
                   attempts INTEGER NOT NULL DEFAULT 0,   -- 阶段4：瞬时错误已尝试次数
                   created_at TEXT DEFAULT (datetime('now', 'localtime')),
                   updated_at TEXT DEFAULT (datetime('now', 'localtime'))
               )"""
        )
        # 迁移：旧库（阶段 3 建）缺阶段 4/5 新列时补齐
        cols = {r["name"] for r in c.execute("PRAGMA table_info(transfer_tasks)")}
        for col, decl in (("target_fid", "TEXT NOT NULL DEFAULT ''"),
                          ("result_json", "TEXT NOT NULL DEFAULT ''"),
                          ("quark_task_id", "TEXT NOT NULL DEFAULT ''"),
                          ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                          ("mode", "TEXT NOT NULL DEFAULT 'episode'"),
                          ("picks", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:
                c.execute(f"ALTER TABLE transfer_tasks ADD COLUMN {col} {decl}")
        # 多用户（阶段 8）：任务归属。'' 为旧版全局任务（无用户上下文）。
        if "user_id" not in cols:
            c.execute("ALTER TABLE transfer_tasks ADD COLUMN "
                      "user_id TEXT NOT NULL DEFAULT ''")
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   id TEXT PRIMARY KEY,              -- uuid4 hex
                   email TEXT NOT NULL UNIQUE,       -- 登录名（暂不做邮箱验证）
                   password_hash TEXT NOT NULL,      -- pbkdf2 迭代哈希
                   role TEXT NOT NULL DEFAULT 'user',    -- user / admin
                   tier TEXT NOT NULL DEFAULT 'lite',    -- lite（每日配额）/ pro
                   stream_token TEXT NOT NULL UNIQUE,    -- 每用户 Stremio Add-on 地址令牌
                   save_dir TEXT NOT NULL DEFAULT '',    -- 用户自己的转存保存目录（空=服务器默认）
                   tmdb_key_enc TEXT NOT NULL DEFAULT '',-- 已废弃：TMDB key 由管理员统一配置
                   disabled INTEGER NOT NULL DEFAULT 0,
                   created_at TEXT DEFAULT (datetime('now', 'localtime'))
               )"""
        )
        # 迁移：旧库 users 缺 save_dir 列时补齐
        ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "save_dir" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN save_dir TEXT NOT NULL DEFAULT ''")
        c.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                   token_hash TEXT PRIMARY KEY,      -- sha256(token) 十六进制
                   user_id TEXT NOT NULL,
                   expires_at REAL NOT NULL          -- unix ts
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS user_cookies (
                   user_id TEXT PRIMARY KEY,
                   cookie_enc TEXT NOT NULL,
                   updated_at TEXT DEFAULT (datetime('now', 'localtime'))
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS daily_usage (
                   user_id TEXT NOT NULL,
                   day TEXT NOT NULL,                -- 'YYYY-MM-DD'（本地时区）
                   search_count INTEGER NOT NULL DEFAULT 0,
                   transfer_count INTEGER NOT NULL DEFAULT 0,
                   play_count INTEGER NOT NULL DEFAULT 0,
                   PRIMARY KEY (user_id, day)
               )"""
        )
        # 迁移：旧库 daily_usage 缺列时补齐
        dcols = {r["name"] for r in c.execute("PRAGMA table_info(daily_usage)")}
        if "transfer_count" not in dcols:
            c.execute("ALTER TABLE daily_usage "
                      "ADD COLUMN transfer_count INTEGER NOT NULL DEFAULT 0")
        if "play_count" not in dcols:
            c.execute("ALTER TABLE daily_usage "
                      "ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0")
        c.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                   key TEXT PRIMARY KEY,             -- 管理员可编辑配置（白名单）
                   value TEXT NOT NULL               -- JSON 序列化
               )"""
        )


def _encrypt(cookie: str) -> str:
    return _fernet.encrypt(cookie.encode()).decode()


def _decrypt(blob: str) -> str:
    return _fernet.decrypt(blob.encode()).decode()


# ---- 转存任务（阶段 3：创建与查询；阶段 4：状态机推进） ----

def find_active_task(db_path: str, *, share_url: str, imdb_id: str,
                     season: int | None, episode: int | None,
                     picks: list[str] | None, user_id: str) -> dict | None:
    """查找幂等键相同的活跃任务（供路由在配额校验前判断"本次是否为复用"）。"""
    picks_key = json.dumps(sorted(picks), ensure_ascii=False) if picks else ""
    with _lock, _conn(db_path) as c:
        row = c.execute(
            """SELECT * FROM transfer_tasks
               WHERE share_url = ? AND imdb_id = ? AND user_id = ?
                 AND IFNULL(season, -1) = IFNULL(?, -1)
                 AND IFNULL(episode, -1) = IFNULL(?, -1)
                 AND picks = ?
                 AND status IN ('PENDING', 'SUBMITTING', 'PROCESSING')""",
            (share_url, imdb_id, user_id, season, episode, picks_key),
        ).fetchone()
    return dict(row) if row else None


def create_task(db_path: str, *, vtype: str, imdb_id: str, season: int | None,
                episode: int | None, title: str, candidate_title: str,
                share_url: str, passcode: str = "", source: str = "",
                mode: str = "episode", picks: list[str] | None = None,
                user_id: str = "") -> tuple[dict, bool]:
    """创建转存任务。幂等：同用户 + 同分享链接 + 同影片/集数 + 同 picks 存在活跃任务时直接复用。

    mode="whole" 表示整包转存（season/episode 记为 None）；
    mode="picks" 表示按所选 fid 列表转存（picks 排序后 JSON 作为幂等键的一部分）；
    user_id 为空串表示旧版全局任务（无用户上下文）；
    返回 (task, created)。
    """
    picks_key = json.dumps(sorted(picks), ensure_ascii=False) if picks else ""
    with _lock, _conn(db_path) as c:
        row = c.execute(
            """SELECT * FROM transfer_tasks
               WHERE share_url = ? AND imdb_id = ? AND user_id = ?
                 AND IFNULL(season, -1) = IFNULL(?, -1)
                 AND IFNULL(episode, -1) = IFNULL(?, -1)
                 AND picks = ?
                 AND status IN ('PENDING', 'SUBMITTING', 'PROCESSING')""",
            (share_url, imdb_id, user_id, season, episode, picks_key),
        ).fetchone()
        if row:
            return dict(row), False
        task_id = uuid.uuid4().hex
        c.execute(
            """INSERT INTO transfer_tasks
                   (id, video_type, imdb_id, season, episode, title,
                    candidate_title, share_url, passcode, source, mode, picks,
                    user_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')""",
            (task_id, vtype, imdb_id, season, episode, title,
             candidate_title, share_url, passcode, source, mode, picks_key,
             user_id),
        )
    return get_task(db_path, task_id), True


def get_task(db_path: str, task_id: str) -> dict | None:
    with _lock, _conn(db_path) as c:
        row = c.execute("SELECT * FROM transfer_tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(db_path: str, limit: int = 50) -> list[dict]:
    with _lock, _conn(db_path) as c:
        rows = c.execute(
            "SELECT * FROM transfer_tasks ORDER BY created_at DESC, id LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def list_tasks_by_user(db_path: str, user_id: str, limit: int = 50) -> list[dict]:
    """某用户的任务列表（创建时间倒序），用户面板展示用。"""
    with _lock, _conn(db_path) as c:
        rows = c.execute(
            """SELECT * FROM transfer_tasks WHERE user_id = ?
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def count_tasks_grouped(db_path: str) -> dict[str, int]:
    """按 user_id 统计任务数（管理页用户列表用）。"""
    with _lock, _conn(db_path) as c:
        rows = c.execute(
            "SELECT user_id, COUNT(*) AS n FROM transfer_tasks GROUP BY user_id"
        ).fetchall()
    return {r["user_id"]: r["n"] for r in rows}


def update_task_status(db_path: str, task_id: str, status: str, error: str = "") -> bool:
    """推进任务状态（阶段 4 使用）。status 必须为六态之一，返回是否更新成功。"""
    if status not in TASK_STATUSES:
        raise ValueError(f"非法任务状态: {status}")
    with _lock, _conn(db_path) as c:
        cur = c.execute(
            """UPDATE transfer_tasks
               SET status = ?, error = ?, updated_at = datetime('now', 'localtime')
               WHERE id = ?""",
            (status, error, task_id),
        )
    return cur.rowcount > 0


_TASK_FIELDS = frozenset({"target_fid", "result_json", "quark_task_id",
                          "attempts", "error"})


def set_task_fields(db_path: str, task_id: str, **fields) -> bool:
    """更新任务的扩展字段（白名单校验），供 Worker 记录夸克任务 ID、定位结果等。"""
    bad = set(fields) - _TASK_FIELDS
    if bad:
        raise ValueError(f"非法任务字段: {bad}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _lock, _conn(db_path) as c:
        cur = c.execute(
            f"""UPDATE transfer_tasks
                SET {sets}, updated_at = datetime('now', 'localtime')
                WHERE id = ?""",
            (*fields.values(), task_id),
        )
    return cur.rowcount > 0


def claim_task(db_path: str, task_id: str, from_status: str, to_status: str,
               error: str = "") -> bool:
    """原子状态认领：仅当当前状态为 from_status 时推进到 to_status。"""
    if to_status not in TASK_STATUSES:
        raise ValueError(f"非法任务状态: {to_status}")
    with _lock, _conn(db_path) as c:
        cur = c.execute(
            """UPDATE transfer_tasks
               SET status = ?, error = ?, updated_at = datetime('now', 'localtime')
               WHERE id = ? AND status = ?""",
            (to_status, error, task_id, from_status),
        )
    return cur.rowcount > 0


def list_tasks_by_status(db_path: str, statuses: list[str]) -> list[dict]:
    qs = ",".join("?" for _ in statuses)
    with _lock, _conn(db_path) as c:
        rows = c.execute(
            f"SELECT * FROM transfer_tasks WHERE status IN ({qs}) "
            "ORDER BY created_at, id", statuses).fetchall()
    return [dict(r) for r in rows]


def get_tasks_for_video(db_path: str, vtype: str, imdb_id: str,
                        season: int | None, episode: int | None,
                        user_id: str | None = None) -> list[dict]:
    """按影片（含季集）查询任务，创建时间倒序——Stream 引导流展示进度用。

    整包任务（mode='whole'）覆盖该影片全部集数，一并返回。
    user_id=None 不过滤（兼容旧调用）；'' 仅旧版全局任务；其他值按用户隔离。"""
    sql = """SELECT * FROM transfer_tasks
             WHERE video_type = ? AND imdb_id = ?
               AND ((IFNULL(season, -1) = IFNULL(?, -1)
                     AND IFNULL(episode, -1) = IFNULL(?, -1)) OR mode = 'whole')"""
    params: list = [vtype, imdb_id, season, episode]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT 5"
    with _lock, _conn(db_path) as c:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_tmdb_meta(db_path: str, imdb_id: str, vtype: str,
                  ttl: float = 7 * 86400) -> dict | None:
    """读取 TMDB 元数据缓存（双语名/季集），TTL 过期返回 None。"""
    with _lock, _conn(db_path) as c:
        row = c.execute(
            "SELECT data, updated_at FROM tmdb_cache WHERE imdb_id = ? AND vtype = ?",
            (imdb_id, vtype),
        ).fetchone()
    if not row or time.time() - row["updated_at"] > ttl:
        return None
    try:
        return json.loads(row["data"])
    except ValueError:
        return None


def save_tmdb_meta(db_path: str, imdb_id: str, vtype: str, data: dict) -> None:
    with _lock, _conn(db_path) as c:
        c.execute(
            """INSERT INTO tmdb_cache (imdb_id, vtype, data, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(imdb_id, vtype) DO UPDATE SET
                   data = excluded.data, updated_at = excluded.updated_at""",
            (imdb_id, vtype, json.dumps(data, ensure_ascii=False), time.time()),
        )


# ---- 通用 KV 缓存（搜索结果 / 分享详情，TTL 过期） ----

def get_cache(db_path: str, key: str) -> object | None:
    """读取缓存值（JSON 反序列化）；不存在或已过期返回 None。"""
    with _lock, _conn(db_path) as c:
        row = c.execute(
            "SELECT value, expires_at FROM kv_cache WHERE key = ?", (key,)).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    try:
        return json.loads(row["value"])
    except ValueError:
        return None


def set_cache(db_path: str, key: str, value, ttl: float) -> None:
    """写入缓存（TTL 秒），顺手清理全部过期项。ttl<=0 时为空操作。"""
    if ttl <= 0:
        return
    with _lock, _conn(db_path) as c:
        c.execute(
            """INSERT INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value, expires_at = excluded.expires_at""",
            (key, json.dumps(value, ensure_ascii=False), time.time() + ttl),
        )
        c.execute("DELETE FROM kv_cache WHERE expires_at < ?", (time.time(),))


# ---- 用户体系（阶段 8：注册/登录/隔离 Cookie/配额/运行时配置） ----

_USER_FIELDS = frozenset({"role", "tier", "disabled", "password_hash",
                          "stream_token", "email", "save_dir"})


def create_user(db_path: str, email: str, password_hash: str, role: str,
                tier: str, stream_token: str) -> dict:
    with _lock, _conn(db_path) as c:
        c.execute(
            """INSERT INTO users (id, email, password_hash, role, tier, stream_token)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uuid.uuid4().hex, email, password_hash, role, tier, stream_token),
        )
    return get_user_by_email(db_path, email)


def get_user(db_path: str, user_id: str) -> dict | None:
    with _lock, _conn(db_path) as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(db_path: str, email: str) -> dict | None:
    with _lock, _conn(db_path) as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def get_user_by_stream_token(db_path: str, token: str) -> dict | None:
    with _lock, _conn(db_path) as c:
        row = c.execute(
            "SELECT * FROM users WHERE stream_token = ?", (token,)).fetchone()
    return dict(row) if row else None


def list_users(db_path: str) -> list[dict]:
    with _lock, _conn(db_path) as c:
        rows = c.execute(
            "SELECT * FROM users ORDER BY created_at, id").fetchall()
    return [dict(r) for r in rows]


def count_users(db_path: str) -> int:
    with _lock, _conn(db_path) as c:
        row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return row["n"]


def update_user(db_path: str, user_id: str, **fields) -> bool:
    """更新用户字段（白名单校验）。返回用户是否存在。"""
    bad = set(fields) - _USER_FIELDS
    if bad:
        raise ValueError(f"非法用户字段: {bad}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _lock, _conn(db_path) as c:
        cur = c.execute(f"UPDATE users SET {sets} WHERE id = ?",
                        (*fields.values(), user_id))
    return cur.rowcount > 0


def delete_user(db_path: str, user_id: str) -> bool:
    """删除用户及其会话/Cookie/用量记录（转存任务保留作审计）。"""
    with _lock, _conn(db_path) as c:
        cur = c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        if cur.rowcount:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM user_cookies WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM daily_usage WHERE user_id = ?", (user_id,))
    return cur.rowcount > 0


# ---- 会话（token 明文不落库，仅存 sha256） ----

def create_session(db_path: str, token: str, user_id: str, ttl: float) -> None:
    from .auth import token_hash
    with _lock, _conn(db_path) as c:
        c.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash(token), user_id, time.time() + ttl),
        )


def get_session_user(db_path: str, token: str) -> dict | None:
    """按会话 token 取用户；过期/用户被禁用返回 None。"""
    from .auth import token_hash
    with _lock, _conn(db_path) as c:
        row = c.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0""",
            (token_hash(token), time.time()),
        ).fetchone()
    return dict(row) if row else None


def delete_session(db_path: str, token: str) -> None:
    from .auth import token_hash
    with _lock, _conn(db_path) as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def delete_user_sessions(db_path: str, user_id: str) -> None:
    with _lock, _conn(db_path) as c:
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# ---- 每用户网盘 Cookie ----

def save_user_cookie(db_path: str, user_id: str, cookie: str) -> None:
    with _lock, _conn(db_path) as c:
        c.execute(
            """INSERT INTO user_cookies (user_id, cookie_enc, updated_at)
               VALUES (?, ?, datetime('now', 'localtime'))
               ON CONFLICT(user_id) DO UPDATE SET
                   cookie_enc = excluded.cookie_enc,
                   updated_at = excluded.updated_at""",
            (user_id, _encrypt(cookie)),
        )


def load_user_cookie(db_path: str, user_id: str) -> str | None:
    with _lock, _conn(db_path) as c:
        row = c.execute(
            "SELECT cookie_enc FROM user_cookies WHERE user_id = ?",
            (user_id,)).fetchone()
    return _decrypt(row["cookie_enc"]) if row else None


def delete_user_cookie(db_path: str, user_id: str) -> bool:
    with _lock, _conn(db_path) as c:
        cur = c.execute("DELETE FROM user_cookies WHERE user_id = ?", (user_id,))
    return cur.rowcount > 0


# ---- 每日搜索/转存配额（本地时区按天计） ----

def incr_search_count(db_path: str, user_id: str) -> int:
    """当日搜索次数 +1，返回累计值。"""
    day = time.strftime("%Y-%m-%d")
    with _lock, _conn(db_path) as c:
        row = c.execute(
            """INSERT INTO daily_usage (user_id, day, search_count) VALUES (?, ?, 1)
               ON CONFLICT(user_id, day) DO UPDATE SET
                   search_count = search_count + 1
               RETURNING search_count""",
            (user_id, day),
        ).fetchone()
    return row["search_count"]


def get_search_count(db_path: str, user_id: str, day: str | None = None) -> int:
    day = day or time.strftime("%Y-%m-%d")
    with _lock, _conn(db_path) as c:
        row = c.execute(
            "SELECT search_count FROM daily_usage WHERE user_id = ? AND day = ?",
            (user_id, day)).fetchone()
    return row["search_count"] if row else 0


def get_search_total(db_path: str, user_id: str) -> int:
    """该用户历史累计搜索次数（全部日期求和）。"""
    with _lock, _conn(db_path) as c:
        row = c.execute(
            "SELECT COALESCE(SUM(search_count), 0) AS s FROM daily_usage "
            "WHERE user_id = ?", (user_id,)).fetchone()
    return row["s"] if row else 0


def incr_transfer_count(db_path: str, user_id: str) -> int:
    """当日转存次数 +1（任务真正新建时计，幂等复用不计），返回累计值。"""
    day = time.strftime("%Y-%m-%d")
    with _lock, _conn(db_path) as c:
        row = c.execute(
            """INSERT INTO daily_usage (user_id, day, transfer_count)
               VALUES (?, ?, 1)
               ON CONFLICT(user_id, day) DO UPDATE SET
                   transfer_count = transfer_count + 1
               RETURNING transfer_count""",
            (user_id, day),
        ).fetchone()
    return row["transfer_count"]


def get_transfer_count(db_path: str, user_id: str, day: str | None = None) -> int:
    day = day or time.strftime("%Y-%m-%d")
    with _lock, _conn(db_path) as c:
        row = c.execute(
            "SELECT transfer_count FROM daily_usage WHERE user_id = ? AND day = ?",
            (user_id, day)).fetchone()
    return row["transfer_count"] if row else 0


def get_transfer_total(db_path: str, user_id: str) -> int:
    """该用户历史累计转存次数（全部日期求和）。"""
    with _lock, _conn(db_path) as c:
        row = c.execute(
            "SELECT COALESCE(SUM(transfer_count), 0) AS s FROM daily_usage "
            "WHERE user_id = ?", (user_id,)).fetchone()
    return row["s"] if row else 0


def incr_play_count(db_path: str, user_id: str) -> int:
    """当日播放次数 +1（Stremio 请求命中可用流时计），返回累计值。"""
    day = time.strftime("%Y-%m-%d")
    with _lock, _conn(db_path) as c:
        row = c.execute(
            """INSERT INTO daily_usage (user_id, day, play_count) VALUES (?, ?, 1)
               ON CONFLICT(user_id, day) DO UPDATE SET
                   play_count = play_count + 1
               RETURNING play_count""",
            (user_id, day),
        ).fetchone()
    return row["play_count"]


def get_play_count(db_path: str, user_id: str, day: str | None = None) -> int:
    day = day or time.strftime("%Y-%m-%d")
    with _lock, _conn(db_path) as c:
        row = c.execute(
            "SELECT play_count FROM daily_usage WHERE user_id = ? AND day = ?",
            (user_id, day)).fetchone()
    return row["play_count"] if row else 0


def get_play_total(db_path: str, user_id: str) -> int:
    """该用户历史累计播放次数（全部日期求和）。"""
    with _lock, _conn(db_path) as c:
        row = c.execute(
            "SELECT COALESCE(SUM(play_count), 0) AS s FROM daily_usage "
            "WHERE user_id = ?", (user_id,)).fetchone()
    return row["s"] if row else 0


# ---- 运行时可编辑配置（管理员面板） ----

def get_setting(db_path: str, key: str):
    with _lock, _conn(db_path) as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except ValueError:
        return None


def set_setting(db_path: str, key: str, value) -> None:
    with _lock, _conn(db_path) as c:
        c.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, json.dumps(value, ensure_ascii=False)),
        )


def all_settings(db_path: str) -> dict:
    with _lock, _conn(db_path) as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except ValueError:
            continue
    return out
