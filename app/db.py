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
            """CREATE TABLE IF NOT EXISTS credentials (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   cookie_enc TEXT NOT NULL,
                   updated_at TEXT DEFAULT (datetime('now', 'localtime'))
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS tmdb_cache (
                   imdb_id TEXT NOT NULL,
                   vtype TEXT NOT NULL,
                   data TEXT NOT NULL,            -- JSON：双语名/年份/季集
                   updated_at REAL NOT NULL       -- unix ts，用于 TTL 过期
               , PRIMARY KEY (imdb_id, vtype))"""
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
                   mode TEXT NOT NULL DEFAULT 'episode',  -- 阶段5：episode 指定集 / whole 整包
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
                          ("mode", "TEXT NOT NULL DEFAULT 'episode'")):
            if col not in cols:
                c.execute(f"ALTER TABLE transfer_tasks ADD COLUMN {col} {decl}")


def _encrypt(cookie: str) -> str:
    return _fernet.encrypt(cookie.encode()).decode()


def _decrypt(blob: str) -> str:
    return _fernet.decrypt(blob.encode()).decode()


def save_cookie(db_path: str, cookie: str) -> None:
    with _lock, _conn(db_path) as c:
        c.execute(
            """INSERT INTO credentials (id, cookie_enc, updated_at)
               VALUES (1, ?, datetime('now', 'localtime'))
               ON CONFLICT(id) DO UPDATE SET
                   cookie_enc = excluded.cookie_enc,
                   updated_at = excluded.updated_at""",
            (_encrypt(cookie),),
        )


def load_cookie(db_path: str) -> str | None:
    with _lock, _conn(db_path) as c:
        row = c.execute("SELECT cookie_enc FROM credentials WHERE id = 1").fetchone()
    return _decrypt(row["cookie_enc"]) if row else None


# ---- 转存任务（阶段 3：创建与查询；阶段 4：状态机推进） ----

def create_task(db_path: str, *, vtype: str, imdb_id: str, season: int | None,
                episode: int | None, title: str, candidate_title: str,
                share_url: str, passcode: str = "", source: str = "",
                mode: str = "episode") -> tuple[dict, bool]:
    """创建转存任务。幂等：同分享链接 + 同影片/集数存在活跃任务时直接复用。

    mode="whole" 表示整包转存（season/episode 记为 None）；返回 (task, created)。
    """
    with _lock, _conn(db_path) as c:
        row = c.execute(
            """SELECT * FROM transfer_tasks
               WHERE share_url = ? AND imdb_id = ?
                 AND IFNULL(season, -1) = IFNULL(?, -1)
                 AND IFNULL(episode, -1) = IFNULL(?, -1)
                 AND status IN ('PENDING', 'SUBMITTING', 'PROCESSING')""",
            (share_url, imdb_id, season, episode),
        ).fetchone()
        if row:
            return dict(row), False
        task_id = uuid.uuid4().hex
        c.execute(
            """INSERT INTO transfer_tasks
                   (id, video_type, imdb_id, season, episode, title,
                    candidate_title, share_url, passcode, source, mode, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')""",
            (task_id, vtype, imdb_id, season, episode, title,
             candidate_title, share_url, passcode, source, mode),
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
                        season: int | None, episode: int | None) -> list[dict]:
    """按影片（含季集）查询任务，创建时间倒序——Stream 引导流展示进度用。

    整包任务（mode='whole'）覆盖该影片全部集数，一并返回。"""
    with _lock, _conn(db_path) as c:
        rows = c.execute(
            """SELECT * FROM transfer_tasks
               WHERE video_type = ? AND imdb_id = ?
                 AND ((IFNULL(season, -1) = IFNULL(?, -1)
                       AND IFNULL(episode, -1) = IFNULL(?, -1)) OR mode = 'whole')
               ORDER BY created_at DESC, id DESC LIMIT 5""",
            (vtype, imdb_id, season, episode),
        ).fetchall()
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
