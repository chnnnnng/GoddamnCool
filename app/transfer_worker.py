"""转存 Worker（阶段 4）：后台消费 transfer_tasks，驱动六态状态机。

状态流转（需求第 7 条）：
  PENDING → SUBMITTING → PROCESSING → COMPLETED
                 ↘ FAILED（永久错误：分享失效/提取码错误/登录态失效/重试超限）
  PROCESSING 轮询超时 → EXPIRED
  瞬时错误（网络/5xx/未分类接口错误）：attempts+1 回 PENDING 重试，超限 FAILED

重启恢复：SUBMITTING 未拿到 quark_task_id → 回 PENDING 重跑（提交前有防重检查）；
         SUBMITTING 已有 quark_task_id / PROCESSING → 直接续轮询。

分享内文件定位策略：
  1. MovieMatcher 精准匹配（标题相似度 + 季集硬条件）→ 转存单个文件；
  2. 集数启发式回退（文件名无标题仅有集数且唯一）→ 转存单个文件；
  3. 均失败 → 转存整个分享顶层（用户确认的兜底策略），匹配交给 Stream 阶段。
提交前防重：网盘已有同名（且大小一致）文件 → 跳过转存直接 COMPLETED。
"""
import asyncio
import json
import logging
import re
import time

from . import config, db
from .tmdb import VideoMeta, fetch_meta
from .matcher import MovieMatcher, JUNK_PATTERN, episode_of, parse
from .providers.base import CloudFile, ProviderAuthError, ProviderError, ShareFile
from .providers.quark import QuarkDriveProvider

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".flv", ".webm", ".m2ts", ".mov", ".wmv", ".m4v"}

_PWD_ID_RE = re.compile(r"pan\.quark\.cn/s/([0-9a-zA-Z]+)")

# 任务终态（进入后不再被 Worker 触碰）
_TERMINAL = {"COMPLETED", "FAILED", "EXPIRED"}


class PermanentError(Exception):
    """不可恢复错误：直接 FAILED，不再重试。"""


def parse_pwd_id(share_url: str) -> str:
    m = _PWD_ID_RE.search(share_url)
    if not m:
        raise PermanentError(f"无法从分享链接解析 pwd_id: {share_url}")
    return m.group(1)


class TransferWorker:
    """单任务串行执行（限流，需求第 10 条）；FastAPI lifespan 内启动。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.DB_PATH
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ---- 生命周期 ----

    def start(self) -> None:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="transfer-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("转存 Worker 停止异常")
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("转存 Worker 扫描异常")
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=config.TRANSFER_SCAN_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        self._recover()
        for task in db.list_tasks_by_status(self.db_path, ["PENDING", "PROCESSING"]):
            if self._stop.is_set():
                return
            try:
                await self._process(task)
            except PermanentError as e:
                self._fail_permanent(task, str(e))
            except ProviderAuthError as e:
                self._fail_permanent(task, f"网盘登录态失效，请重新在 /setup 授权: {e}")
            except Exception:
                # 未预期异常按瞬时错误处理（重试机制兜底）
                log.exception("任务 %s 执行异常", task["id"])
                self._fail_transient(task, "内部异常")

    def _recover(self) -> None:
        """服务重启后的任务恢复（需求第 7 条：重启后可恢复未完成任务）。"""
        for t in db.list_tasks_by_status(self.db_path, ["SUBMITTING"]):
            if t["quark_task_id"]:
                db.claim_task(self.db_path, t["id"], "SUBMITTING", "PROCESSING",
                              error="服务重启，续查夸克任务")
            else:
                db.claim_task(self.db_path, t["id"], "SUBMITTING", "PENDING",
                              error="服务重启，重新执行")
        for t in db.list_tasks_by_status(self.db_path, ["PROCESSING"]):
            if not t["quark_task_id"]:
                db.claim_task(self.db_path, t["id"], "PROCESSING", "PENDING",
                              error="服务重启，缺少夸克任务 ID，重新执行")

    # ---- 单任务处理 ----

    async def _process(self, task: dict) -> None:
        if task["status"] == "PENDING":
            if not db.claim_task(self.db_path, task["id"], "PENDING", "SUBMITTING",
                                 error="正在解析分享资源"):
                return
            task = dict(task, status="SUBMITTING")
            await self._submit(task)
        elif task["status"] == "PROCESSING" and task["quark_task_id"]:
            await self._poll_and_finish(task)

    async def _submit(self, task: dict) -> None:
        """SUBMITTING：解析分享 → 选文件 → 防重 → 提交转存 → PROCESSING。"""
        cookie = db.load_cookie(self.db_path) or (config.QUARK_COOKIE or None)
        if not cookie:
            raise PermanentError("未绑定网盘账号，请访问 /setup 完成授权")
        provider = QuarkDriveProvider(cookie=cookie, db_path=self.db_path)
        try:
            pwd_id = parse_pwd_id(task["share_url"])
            try:
                stoken = await provider.get_share_stoken(pwd_id, task["passcode"])
            except ProviderAuthError:
                raise
            except ProviderError as e:
                # token 阶段失败均为永久性：分享失效/提取码错误/接口形态变化
                raise PermanentError(f"分享访问失败: {e}") from e
            entries = await provider.list_share_files(pwd_id, stoken)
            meta = await self._load_meta(task)
            mode = task.get("mode") or "episode"
            if mode == "whole":  # 转存完整剧集：跳过选片，整包落盘（阶段 5）
                tops = [e for e in entries if "/" not in e.path]
                if not tops:
                    raise PermanentError("分享内容为空")
                chosen, result = tops, {"mode": "bulk", "share_entries": len(tops),
                                        "names": [t.path for t in tops][:20]}
            elif mode == "picks":  # 转存所选剧集（可多选）：按 fid 列表转存（阶段 6）
                chosen, result = await self._pick_files_by_fids(provider, task, entries)
                if not chosen:  # 全部防重命中：网盘已有所选文件
                    return self._complete_picks_dedup(task)
            else:
                picked = self._pick_file(entries, meta, task)
                if picked is not None:
                    existing = await self._find_duplicate(provider, picked)
                    if existing:
                        log.info("任务 %s 防重命中，网盘已有 %s，跳过转存",
                                 task["id"], picked.name)
                        return self._complete(task, fid=existing, located="dedup",
                                              mode="targeted", name=picked.name,
                                              size=picked.size)
                    chosen = [picked]
                    result = {"mode": "targeted", "name": picked.name,
                              "size": picked.size, "path": picked.path}
                else:
                    tops = [e for e in entries if "/" not in e.path]
                    if not tops:
                        raise PermanentError("分享内容为空")
                    chosen = tops
                    result = {"mode": "bulk", "share_entries": len(tops),
                              "names": [t.path for t in tops][:20]}

            save = await provider.save_share_files(
                pwd_id, stoken, chosen, to_pdir_fid=await self._target_dir(provider))
            # save 响应内嵌的 top_fids 是轮询响应缺失时的兜底（实测出现过 null）
            db.set_task_fields(self.db_path, task["id"],
                               quark_task_id=save["task_id"],
                               result_json=json.dumps(
                                   dict(result, save_top_fids=save["top_fids"]),
                                   ensure_ascii=False))
            if db.claim_task(self.db_path, task["id"], "SUBMITTING", "PROCESSING",
                             error="夸克转存处理中"):
                task = dict(task, status="PROCESSING",
                            quark_task_id=save["task_id"],
                            result_json=json.dumps(
                                dict(result, save_top_fids=save["top_fids"])))
                await self._poll_and_finish(task)
        finally:
            await provider.close()

    async def _poll_and_finish(self, task: dict) -> None:
        """PROCESSING：轮询夸克任务直至成功/失败/超时。"""
        cookie = db.load_cookie(self.db_path) or (config.QUARK_COOKIE or None)
        if not cookie:
            raise PermanentError("未绑定网盘账号，请访问 /setup 完成授权")
        provider = QuarkDriveProvider(cookie=cookie, db_path=self.db_path)
        deadline = time.monotonic() + config.TRANSFER_TASK_TIMEOUT
        try:
            ridx = 0
            while True:
                info = await provider.get_transfer_task(task["quark_task_id"],
                                                        retry_index=ridx)
                if info["status"] == 2:  # 实测：2=成功
                    return await self._locate_and_complete(provider, task, info)
                if info["status"] not in (0, 1):
                    raise PermanentError(
                        f"夸克转存任务失败 (status={info['status']})")
                if time.monotonic() >= deadline:
                    db.update_task_status(self.db_path, task["id"], "EXPIRED",
                                          error=f"转存轮询超时（{int(config.TRANSFER_TASK_TIMEOUT)}s）")
                    return
                ridx += 1
                await asyncio.sleep(config.TRANSFER_POLL_INTERVAL)
        finally:
            await provider.close()

    # ---- 文件选择 / 防重 / 定位 ----

    async def _pick_files_by_fids(
            self, provider: QuarkDriveProvider, task: dict,
            entries: list[ShareFile]) -> tuple[list[ShareFile], dict]:
        """picks 模式：按任务记录的 fid 列表选取分享文件；逐个防重，
        网盘已有的跳过。返回 (待转存文件列表, result)。"""
        want = set(json.loads(task["picks"] or "[]"))
        videos = [e for e in entries if not e.is_dir and e.fid in want]
        if not videos:
            raise PermanentError("所选文件不在分享内容中（分享可能已更新）")
        chosen = []
        for f in videos:
            if await self._find_duplicate(provider, f):
                log.info("任务 %s 防重命中 %s，跳过", task["id"], f.name)
            else:
                chosen.append(f)
        result = {"mode": "picks",
                  "names": [f.path for f in videos][:50],
                  "skipped": [f.path for f in videos if f not in chosen]}
        return chosen, result

    def _complete_picks_dedup(self, task: dict) -> None:
        """picks 模式全部防重命中：无需转存，直接 COMPLETED。"""
        db.set_task_fields(self.db_path, task["id"], target_fid="",
                           result_json=json.dumps(
                               {"mode": "picks", "located": "dedup", "fid": ""},
                               ensure_ascii=False))
        db.update_task_status(self.db_path, task["id"], "COMPLETED",
                              error="网盘已有所选文件，跳过重复转存")

    async def _load_meta(self, task: dict) -> VideoMeta:
        meta = await fetch_meta(task["imdb_id"], task["video_type"], self.db_path)
        if meta is not None and meta.title:
            return meta
        # TMDB 失败时退回任务里缓存的标题（无年份，匹配降级但不中断）
        return VideoMeta({"imdb_id": task["imdb_id"], "name": task["title"],
                          "type": task["video_type"]})

    @staticmethod
    def _pick_file(entries: list[ShareFile], meta: VideoMeta,
                   task: dict) -> ShareFile | None:
        """从分享条目中定位目标文件：Matcher → 集数启发式 → None（整包转存）。"""
        videos = [e for e in entries
                  if not e.is_dir and e.name
                  and parse(e.name).ext in VIDEO_EXTS
                  and not JUNK_PATTERN.search(e.name)]
        if not videos:
            return None
        cloud = [CloudFile(fid=e.fid, name=e.name, size=e.size, is_dir=False)
                 for e in videos]
        best = MovieMatcher(config.MATCH_THRESHOLD).best(
            cloud, meta, task["season"], task["episode"])
        if best is not None:
            return next(e for e in videos if e.fid == best.file.fid)

        # 集数启发式：文件名无有效标题但集数唯一命中（如 "06.mkv"）
        if task["episode"] is not None:
            hits = []
            for e in videos:
                ep = episode_of(e.name)
                if ep != task["episode"]:
                    continue
                p = parse(e.name)
                if p.season is not None and task["season"] is not None \
                        and p.season != task["season"]:
                    continue
                hits.append(e)
            if len(hits) == 1:
                log.info("Matcher 未命中，按集数回退选中: %s", hits[0].path)
                return hits[0]
        return None

    @staticmethod
    async def _find_duplicate(provider: QuarkDriveProvider,
                              share_file: ShareFile) -> str | None:
        """防重：网盘已存在同名且大小一致的文件时返回其 fid（搜索不返回目录）。"""
        try:
            found = await provider.search_files(share_file.name)
        except ProviderError:
            return None  # 搜索失败不阻塞转存
        for f in found:
            if f.name == share_file.name and \
               (not share_file.size or not f.size or f.size == share_file.size):
                return f.fid
        return None

    async def _target_dir(self, provider: QuarkDriveProvider) -> str:
        """解析转存目标目录 fid（GODDAMNCOOL）；未配置目录名则转存到根目录。"""
        if not config.TRANSFER_DIR:
            return "0"
        return await provider.find_or_create_dir(config.TRANSFER_DIR)

    async def _locate_and_complete(self, provider: QuarkDriveProvider,
                                   task: dict, info: dict) -> None:
        """转存成功后定位最终文件：轮询 top_fids → save 内嵌 top_fids → 网盘搜索。"""
        try:
            result = json.loads(task["result_json"]) if task["result_json"] else {}
        except ValueError:
            result = {}
        mode = result.get("mode", "targeted")
        top_fids = info["top_fids"] or list(result.get("save_top_fids") or [])
        fid, located = "", "save_as"

        if mode == "targeted":
            if len(top_fids) == 1:
                fid = top_fids[0]  # 单文件转存：唯一落盘 fid 即目标文件
            else:
                found = await self._search_safe(provider,
                                                result.get("name") or task["title"])
                if found:
                    fid, located = found[0].fid, "search"
                elif top_fids:
                    located = "save_as_multi"
                else:
                    raise PermanentError("转存完成但未能在网盘定位到文件")
        else:  # bulk/picks：多文件落盘，播放定位交给 Stream 匹配，不锚定单文件
            found: list[CloudFile] = []
            if not top_fids:
                found = await self._search_safe(provider, task["title"])
                if not found:
                    raise PermanentError("整包转存完成但未能在网盘定位到文件")
                located = "search"
            if not result.get("files"):  # 记录落盘文件清单（供 Stream 按季集定位）
                if not found:
                    found = await self._search_safe(provider, task["title"])
                result["files"] = [{"name": f.name, "fid": f.fid, "size": f.size}
                                   for f in found[:50]]

        db.set_task_fields(self.db_path, task["id"],
                           target_fid=fid,
                           result_json=json.dumps(dict(
                               result, fid=fid, located=located,
                               top_fids=top_fids), ensure_ascii=False))
        db.update_task_status(self.db_path, task["id"], "COMPLETED",
                              error="转存完成")
        log.info("任务 %s COMPLETED fid=%s mode=%s located=%s",
                 task["id"], fid, mode, located)

    @staticmethod
    async def _search_safe(provider: QuarkDriveProvider, kw: str) -> list[CloudFile]:
        try:
            return await provider.search_files(kw)
        except ProviderError:
            return []  # 搜索兜底失败不推翻夸克已报告的成功

    def _complete(self, task: dict, *, fid: str, located: str, **result) -> None:
        db.set_task_fields(self.db_path, task["id"], target_fid=fid,
                           result_json=json.dumps(
                               dict(result, fid=fid, located=located),
                               ensure_ascii=False))
        db.update_task_status(self.db_path, task["id"], "COMPLETED",
                              error="转存完成")

    # ---- 错误处理 ----

    def _fail_permanent(self, task: dict, msg: str) -> None:
        log.warning("任务 %s FAILED: %s", task["id"], msg)
        db.update_task_status(self.db_path, task["id"], "FAILED", error=msg)

    def _fail_transient(self, task: dict, msg: str) -> None:
        """瞬时错误：attempts+1 回 PENDING 重试；超限 FAILED。

        状态以 DB 当前值为准（task 字典里的 status 在流转后已过期）。"""
        current = db.get_task(self.db_path, task["id"])
        if current is None or current["status"] in _TERMINAL:
            return
        attempts = int(current.get("attempts") or 0) + 1
        db.set_task_fields(self.db_path, task["id"], attempts=attempts)
        if attempts >= config.TRANSFER_MAX_ATTEMPTS:
            self._fail_permanent(task, f"{msg}（已重试 {attempts} 次）")
            return
        db.claim_task(self.db_path, task["id"], current["status"], "PENDING",
                      error=f"瞬时错误待重试({attempts}/{config.TRANSFER_MAX_ATTEMPTS}): {msg}")
        log.info("任务 %s 瞬时错误回 PENDING: %s", task["id"], msg)
