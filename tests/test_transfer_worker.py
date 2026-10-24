"""转存 Worker 测试（阶段 4）：六态状态机、防重、恢复、重试、超时全场景。

用有状态的 FakeQuark 后端（httpx.MockTransport）模拟夸克接口序列：
token → detail → save → task 轮询 → search/sort；TMDB 打桩。
Worker 通过直接驱动 _tick() 验证状态推进，不经真实定时循环。
"""
import json
import os
import sqlite3
import uuid

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from httpx import AsyncClient, MockTransport, Response  # noqa: E402

from app import config, db  # noqa: E402
from app.tmdb import VideoMeta  # noqa: E402
import app.transfer_worker as tw  # noqa: E402
from app.transfer_worker import TransferWorker  # noqa: E402

SECRET = Fernet.generate_key().decode()
SHARE_URL = "https://pan.quark.cn/s/abc123def"


def j(code=0, message="ok", data=None):
    return Response(200, json={"code": code, "message": message, "data": data})


class FakeQuark:
    """最小夸克后端：分享树 / 网盘文件 / 任务序列，均可由测试注入分支。"""

    def __init__(self):
        self.stoken = "st-" + uuid.uuid4().hex[:8]
        self.token_code = 0            # 非 0 → token 直接报此码
        self.fail_saves = 0            # save 端点瞬时 5xx 次数
        self.poll_status = None        # 设置后 /task 恒返回此 status
        self.task_status: dict[str, list[int]] = {}
        self.task_top: dict[str, list[str]] = {}
        self.save_calls: list[dict] = []
        self.drive_files: list[dict] = []
        self.drive_dirs: list[str] = []
        # 分享树：pdir_fid -> 条目列表（默认标题齐全，targeted 命中 E01）
        self.entries = {
            "0": [{"fid": "d1", "file_name": "余罪 第一季", "dir": True,
                   "size": 0, "share_fid_token": "t-d1"}],
            "d1": [
                {"fid": "s1", "file_name": "余罪.S01E01.2016.1080p.WEB-DL.mp4",
                 "dir": False, "size": 800, "share_fid_token": "t-s1"},
                {"fid": "s2", "file_name": "余罪.S01E02.2016.1080p.WEB-DL.mp4",
                 "dir": False, "size": 810, "share_fid_token": "t-s2"},
            ],
        }
        self._by_fid = {e["fid"]: e for lst in self.entries.values() for e in lst}

    # ---- 辅助 ----

    def land(self, fid: str) -> str:
        """模拟转存落盘：分享条目进网盘，返回新 fid。"""
        e = self._by_fid[fid]
        new_fid = f"new-{fid}"
        self.drive_files.append({"fid": new_fid, "file_name": e["file_name"],
                                 "size": e["size"], "dir": False,
                                 "parent_path": "/GODDAMNCOOL"})
        return new_fid

    def handler(self, request) -> Response:
        path = request.url.path
        if path.endswith("/share/sharepage/token"):
            if self.token_code:
                return j(self.token_code, "分享地址已失效")
            return j(data={"stoken": self.stoken})
        if path.endswith("/share/sharepage/detail"):
            return j(data={"list": self.entries.get(request.url.params["pdir_fid"], [])})
        if path.endswith("/share/sharepage/save"):
            if self.fail_saves:
                self.fail_saves -= 1
                return j(15000, "inner error")
            body = json.loads(request.content)
            self.save_calls.append(body)
            tid = f"qt-{len(self.save_calls)}"
            tops = [self.land(f) for f in body["fid_list"]]
            self.task_status[tid] = [1, 2]
            self.task_top[tid] = tops
            return j(data={"task_id": tid, "task_sync": True,
                           "task_resp": {"data": {"status": 1,
                                                  "save_as": {"save_as_top_fids": tops}}}})
        if path.endswith("/task"):
            tid = request.url.params["task_id"]
            if self.poll_status is not None:
                return j(data={"task_id": tid, "status": self.poll_status})
            seq = self.task_status.get(tid, [2])
            st = seq.pop(0) if len(seq) > 1 else seq[0]
            data = {"task_id": tid, "status": st}
            if st == 2:
                data["save_as"] = {"save_as_top_fids": self.task_top.get(tid, [])}
            return j(data=data)
        if path.endswith("/file/search"):
            q = request.url.params["query"]
            return j(data={"list": [f for f in self.drive_files if q in f["file_name"]]})
        if path.endswith("/file/sort"):
            return j(data={"list": [{"fid": f"d-{n}", "file_name": n, "dir": True}
                                    for n in self.drive_dirs]})
        if path.endswith("/file") and request.method == "POST":
            name = json.loads(request.content)["file_name"]
            if name in self.drive_dirs:
                return j(23008, "同名冲突")
            self.drive_dirs.append(name)
            return j(data={"fid": f"d-{name}"})
        return j(1, "unexpected " + path)


# ---- 公共设施 ----

def make_meta():
    return VideoMeta({"imdb_id": "tt5925192", "name": "余罪",
                      "type": "series", "releaseInfo": "2016"})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    db.init(path, SECRET)
    uid = db.create_user(path, "u@test.io", "x", "user", "pro", "tok")["id"]
    db.save_user_cookie(path, uid, "c=1")
    monkeypatch.setattr(config, "TRANSFER_DIR", "GODDAMNCOOL")
    monkeypatch.setattr(config, "TRANSFER_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(config, "TRANSFER_TASK_TIMEOUT", 5)
    monkeypatch.setattr(config, "TRANSFER_MAX_ATTEMPTS", 3)

    fake = FakeQuark()
    real_cls = tw.QuarkDriveProvider

    def factory(cookie, db_path, user_id=""):
        p = real_cls(cookie=cookie, db_path=db_path, user_id=user_id)
        p._client = AsyncClient(transport=MockTransport(fake.handler), timeout=10,
                                headers={"Cookie": cookie}, params={"pr": "ucpro", "fr": "pc"})
        return p

    monkeypatch.setattr(tw, "QuarkDriveProvider", factory)

    async def fake_fetch_meta(*a, **k):
        return make_meta()

    monkeypatch.setattr(tw, "fetch_meta", fake_fetch_meta)
    return fake, path


def make_task(path, season=1, episode=1):
    uid = db.get_user_by_email(path, "u@test.io")["id"]  # 任务须归属绑定 Cookie 的用户
    task, _ = db.create_task(
        path, vtype="series", imdb_id="tt5925192", season=season, episode=episode,
        title="余罪", candidate_title="余罪 1-2季 4K", share_url=SHARE_URL,
        passcode="", source="test", user_id=uid)
    return task


async def run_tick(path):
    w = TransferWorker(db_path=path)
    await w._tick()
    return w


# ---- 场景 ----

async def test_targeted_transfer_success(env):
    fake, path = env
    t = make_task(path)
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "COMPLETED"
    assert t["target_fid"] == "new-s1"          # E01 唯一命中
    result = json.loads(t["result_json"])
    assert result["mode"] == "targeted" and result["located"] == "save_as"
    # 转存进了 GODDAMNCOOL 目录
    assert fake.save_calls[0]["to_pdir_fid"] == "d-GODDAMNCOOL"
    assert fake.drive_dirs == ["GODDAMNCOOL"]


async def test_dedup_still_saves_and_marks(env):
    """网盘已有同名文件：不跳过转存（用户可能已改名/删除），仅标记 existed。"""
    fake, path = env
    fake.drive_files.append({"fid": "own-1", "dir": False, "size": 800,
                             "file_name": "余罪.S01E01.2016.1080p.WEB-DL.mp4",
                             "parent_path": "/"})
    t = make_task(path)
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "COMPLETED"
    assert len(fake.save_calls) == 1              # 照常提交转存
    result = json.loads(t["result_json"])
    assert result["existed"] == ["余罪.S01E01.2016.1080p.WEB-DL.mp4"]


async def test_share_invalid_fails_fast(env):
    fake, path = env
    fake.token_code = 41011
    t = make_task(path)
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "FAILED"
    assert "分享" in t["error"]


async def test_episode_fallback_unique(env):
    """文件名无标题（01.mp4/02.mp4）时按集数启发式唯一命中。"""
    fake, path = env
    fake.entries["d1"] = [
        {"fid": "s1", "file_name": "01.mp4", "dir": False, "size": 800,
         "share_fid_token": "t-s1"},
        {"fid": "s2", "file_name": "02.mp4", "dir": False, "size": 810,
         "share_fid_token": "t-s2"},
    ]
    fake._by_fid = {e["fid"]: e for lst in fake.entries.values() for e in lst}
    t = make_task(path, episode=2)
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "COMPLETED" and t["target_fid"] == "new-s2"


async def test_ambiguous_episode_bulks_whole_share(env):
    """集数歧义（两个 06）→ 转存整个分享顶层。"""
    fake, path = env
    fake.entries = {
        "0": [
            {"fid": "s1", "file_name": "06.mkv", "dir": False, "size": 800,
             "share_fid_token": "t-s1"},
            {"fid": "s2", "file_name": "06(1).mkv", "dir": False, "size": 810,
             "share_fid_token": "t-s2"},
        ],
    }
    fake._by_fid = {e["fid"]: e for lst in fake.entries.values() for e in lst}
    t = make_task(path, episode=2)
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "COMPLETED"
    assert fake.save_calls[0]["fid_list"] == ["s1", "s2"]   # 整包 = 顶层全部
    result = json.loads(t["result_json"])
    assert result["mode"] == "bulk"


async def test_whole_mode_transfers_all_and_records_files(env):
    """whole 模式（转存完整剧集）：跳过选片整包转存，落盘清单写入 result_json.files。"""
    fake, path = env
    fake.entries = {
        "0": [
            {"fid": "s1", "file_name": "余罪.S01E01.2016.1080p.WEB-DL.mp4",
             "dir": False, "size": 800, "share_fid_token": "t-s1"},
            {"fid": "s2", "file_name": "余罪.S01E02.2016.1080p.WEB-DL.mp4",
             "dir": False, "size": 810, "share_fid_token": "t-s2"},
        ],
    }
    fake._by_fid = {e["fid"]: e for lst in fake.entries.values() for e in lst}
    t, _ = db.create_task(path, vtype="series", imdb_id="tt5925192", season=None,
                          episode=None, title="余罪", candidate_title="余罪 全集",
                          share_url=SHARE_URL, mode="whole",
                          user_id=db.get_user_by_email(path, "u@test.io")["id"])
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "COMPLETED"
    assert t["target_fid"] == ""                       # 整包不锚定单文件
    assert fake.save_calls[0]["fid_list"] == ["s1", "s2"]  # 分享顶层全部条目
    result = json.loads(t["result_json"])
    assert result["mode"] == "bulk"
    assert {f["name"]: f["fid"] for f in result["files"]} == {
        "余罪.S01E01.2016.1080p.WEB-DL.mp4": "new-s1",
        "余罪.S01E02.2016.1080p.WEB-DL.mp4": "new-s2",
    }


async def test_picks_mode_transfers_selected_fids(env):
    """picks 模式：按任务 fid 列表转存所选文件（可多选），只转这些。"""
    fake, path = env
    fake.entries = {
        "0": [
            {"fid": "s1", "file_name": "余罪.S01E01.2016.1080p.WEB-DL.mp4",
             "dir": False, "size": 800, "share_fid_token": "t-s1"},
            {"fid": "s2", "file_name": "余罪.S01E02.2016.1080p.WEB-DL.mp4",
             "dir": False, "size": 810, "share_fid_token": "t-s2"},
            {"fid": "s3", "file_name": "余罪.S01E03.2016.1080p.WEB-DL.mp4",
             "dir": False, "size": 820, "share_fid_token": "t-s3"},
        ],
    }
    fake._by_fid = {e["fid"]: e for lst in fake.entries.values() for e in lst}
    t, _ = db.create_task(path, vtype="series", imdb_id="tt5925192", season=None,
                          episode=None, title="余罪", candidate_title="余罪",
                          share_url=SHARE_URL, mode="picks", picks=["s1", "s3"],
                          user_id=db.get_user_by_email(path, "u@test.io")["id"])
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "COMPLETED"
    assert t["target_fid"] == ""                        # 多文件不锚定单文件
    assert fake.save_calls[0]["fid_list"] == ["s1", "s3"]  # 只转所选
    result = json.loads(t["result_json"])
    assert result["mode"] == "picks"
    assert {f["fid"] for f in result["files"]} == {"new-s1", "new-s3"}


async def test_picks_mode_fids_not_in_share_fails(env):
    """picks 模式：所选 fid 不在分享内容中 → 永久失败。"""
    fake, path = env
    t, _ = db.create_task(path, vtype="series", imdb_id="tt5925192", season=None,
                          episode=None, title="余罪", candidate_title="余罪",
                          share_url=SHARE_URL, mode="picks", picks=["nope"],
                          user_id=db.get_user_by_email(path, "u@test.io")["id"])
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "FAILED" and "所选文件" in t["error"]


async def test_picks_mode_existed_marks(env):
    """picks 模式：网盘已有所选文件（同名同大小）→ 照常转存并标记 existed。"""
    fake, path = env
    fake.entries = {
        "0": [
            {"fid": "s1", "file_name": "余罪.S01E01.2016.1080p.WEB-DL.mp4",
             "dir": False, "size": 800, "share_fid_token": "t-s1"},
        ],
    }
    fake._by_fid = {"s1": fake.entries["0"][0]}
    fake.drive_files.append({"fid": "own-1", "dir": False, "size": 800,
                             "file_name": "余罪.S01E01.2016.1080p.WEB-DL.mp4",
                             "parent_path": "/"})
    t, _ = db.create_task(path, vtype="series", imdb_id="tt5925192", season=None,
                          episode=None, title="余罪", candidate_title="余罪",
                          share_url=SHARE_URL, mode="picks", picks=["s1"],
                          user_id=db.get_user_by_email(path, "u@test.io")["id"])
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "COMPLETED"
    assert len(fake.save_calls) == 1              # 不再跳过转存
    assert json.loads(t["result_json"])["existed"] == \
        ["余罪.S01E01.2016.1080p.WEB-DL.mp4"]


async def test_poll_failure_status_fails_task(env):
    fake, path = env
    fake.poll_status = 3
    t = make_task(path)
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "FAILED" and "3" in t["error"]


async def test_poll_timeout_expires_task(env, monkeypatch):
    fake, path = env
    fake.poll_status = 1
    monkeypatch.setattr(config, "TRANSFER_TASK_TIMEOUT", 0.05)
    t = make_task(path)
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "EXPIRED"


async def test_transient_error_retries_then_succeeds(env):
    fake, path = env
    fake.fail_saves = 1
    t = make_task(path)
    await run_tick(path)                       # 第一次：save 瞬时失败
    t = db.get_task(path, t["id"])
    assert t["status"] == "PENDING" and t["attempts"] == 1
    await run_tick(path)                       # 第二次：成功
    t = db.get_task(path, t["id"])
    assert t["status"] == "COMPLETED"


async def test_transient_error_exhausts_attempts(env):
    fake, path = env
    fake.fail_saves = 99
    t = make_task(path)
    for _ in range(3):
        await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "FAILED" and t["attempts"] == 3


async def test_recover_processing_after_restart(env):
    """重启恢复：PROCESSING 带夸克任务 ID → 续轮询至完成。"""
    fake, path = env
    t = make_task(path)
    db.claim_task(path, t["id"], "PENDING", "PROCESSING")
    db.set_task_fields(path, t["id"], quark_task_id="qt-x",
                       result_json=json.dumps(
                           {"mode": "targeted", "name": "余罪.S01E01.2016.1080p.WEB-DL.mp4",
                            "size": 800, "save_top_fids": ["new-s1"]}))
    fake.task_status["qt-x"] = [2]
    fake.task_top["qt-x"] = ["new-s1"]
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "COMPLETED" and t["target_fid"] == "new-s1"


async def test_recover_submitting_without_task_id(env):
    """重启恢复：SUBMITTING 未拿到夸克任务 ID → 回 PENDING 重跑。"""
    fake, path = env
    t = make_task(path)
    db.claim_task(path, t["id"], "PENDING", "SUBMITTING")
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "COMPLETED"          # 重跑后正常完成


async def test_auth_error_fails_permanently(env, monkeypatch):
    """登录态失效 → FAILED 且提示重新授权（不重试）。"""
    from app.providers.base import ProviderAuthError
    from app.providers.quark import QuarkDriveProvider as RealProvider

    fake, path = env
    t = make_task(path)

    async def broken(self, pwd_id, passcode=""):
        raise ProviderAuthError("登录态失效 (code=31001)")

    monkeypatch.setattr(RealProvider, "get_share_stoken", broken)  # 真实类方法（工厂包装的是它）
    await run_tick(path)
    t = db.get_task(path, t["id"])
    assert t["status"] == "FAILED" and "重新绑定" in t["error"]


# ---- db 层 ----

def test_claim_task_is_atomic(tmp_path):
    path = str(tmp_path / "t.db")
    db.init(path, SECRET)
    t, _ = db.create_task(path, vtype="movie", imdb_id="tt1", season=None,
                          episode=None, title="T", candidate_title="c",
                          share_url=SHARE_URL)
    assert db.claim_task(path, t["id"], "PENDING", "SUBMITTING")
    assert not db.claim_task(path, t["id"], "PENDING", "SUBMITTING")  # 已非 PENDING
    assert db.get_task(path, t["id"])["status"] == "SUBMITTING"


def test_set_task_fields_whitelist(tmp_path):
    path = str(tmp_path / "t.db")
    db.init(path, SECRET)
    t, _ = db.create_task(path, vtype="movie", imdb_id="tt1", season=None,
                          episode=None, title="T", candidate_title="c",
                          share_url=SHARE_URL)
    assert db.set_task_fields(path, t["id"], target_fid="f", attempts=2)
    assert db.get_task(path, t["id"])["attempts"] == 2
    with pytest.raises(ValueError):
        db.set_task_fields(path, t["id"], status="COMPLETED")  # 非白名单字段


def test_get_tasks_for_video_matches_season_episode(tmp_path):
    path = str(tmp_path / "t.db")
    db.init(path, SECRET)
    db.create_task(path, vtype="series", imdb_id="tt1", season=1, episode=1,
                   title="T", candidate_title="c", share_url="u1")
    db.create_task(path, vtype="series", imdb_id="tt1", season=1, episode=2,
                   title="T", candidate_title="c", share_url="u2")
    assert len(db.get_tasks_for_video(path, "series", "tt1", 1, 1)) == 1
    assert len(db.get_tasks_for_video(path, "series", "tt1", 1, 2)) == 1
    assert db.get_tasks_for_video(path, "series", "tt1", 1, 3) == []


def test_migration_adds_phase4_columns(tmp_path):
    """阶段 3 旧库（无新列）经 init 迁移后补齐阶段 4 列。"""
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE transfer_tasks (
                       id TEXT PRIMARY KEY, video_type TEXT NOT NULL,
                       imdb_id TEXT NOT NULL, season INTEGER, episode INTEGER,
                       title TEXT NOT NULL, candidate_title TEXT NOT NULL,
                       share_url TEXT NOT NULL, passcode TEXT NOT NULL DEFAULT '',
                       source TEXT NOT NULL DEFAULT '',
                       status TEXT NOT NULL DEFAULT 'PENDING',
                       error TEXT NOT NULL DEFAULT '',
                       created_at TEXT, updated_at TEXT)""")
    con.execute("""INSERT INTO transfer_tasks (id, video_type, imdb_id, title,
                   candidate_title, share_url) VALUES ('old1', 'movie', 'tt1',
                   '旧任务', 'c', 'https://pan.quark.cn/s/old')""")
    con.commit()
    con.close()

    db.init(path, SECRET)
    t = db.get_task(path, "old1")
    assert t["target_fid"] == "" and t["attempts"] == 0  # 旧数据仍在且新列有默认值
    assert t["mode"] == "episode"  # 阶段 5 mode 列同样自动补齐默认值
