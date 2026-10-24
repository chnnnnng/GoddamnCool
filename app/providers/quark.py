"""夸克网盘 Provider。

接口为非官方私有接口（阶段 1 POC 已验证），随夸克版本可能变更；
所有端点细节集中在本文件，便于后续适配。基址与公共参数：
  https://drive-pc.quark.cn/1/clouddrive?pr=ucpro&fr=pc
鉴权：请求头 Cookie（网页版凭证），响应 Set-Cookie 中的 __puus 必须回写。
"""
import asyncio
import logging

import httpx

from .. import db
from .base import (CloudDriveProvider, CloudFile, PlayResource, ProviderAuthError,
                   ProviderError, ShareFile)

log = logging.getLogger(__name__)

BASE = "https://drive-pc.quark.cn/1/clouddrive"
COMMON_PARAMS = {"pr": "ucpro", "fr": "pc"}
REFERER = "https://pan.quark.cn/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 "
    "Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"
)
AUTH_CODES = {31001, 31002, 31003}  # 登录态相关错误码（实测 31001=token 无效）

# /file/v2/play 转码档位排序（原画条目 trans_status=raw，恒为第一优先）
_RESOLUTION_RANK = {"4k": 5, "2k": 4, "super": 3, "high": 2, "low": 1}


class QuarkDriveProvider(CloudDriveProvider):
    name = "夸克网盘"

    def __init__(self, cookie: str, db_path: str):
        self._cookie = cookie.strip()
        self._db_path = db_path
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            headers={
                "Cookie": self._cookie,
                "User-Agent": USER_AGENT,
                "Referer": REFERER,
            },
            params=COMMON_PARAMS,
        )
        self._lock = asyncio.Lock()

    # ---- 内部工具 ----

    def _absorb_puus(self, resp: httpx.Response) -> None:
        """响应携带新 __puus 时回写内存与数据库（限流对抗，关键工程细节）。"""
        puus = resp.cookies.get("__puus")
        if not puus:
            return
        head = self._cookie.split("__puus=")[0].rstrip("; ")
        self._cookie = (head + ";" if head else "") + f"__puus={puus}"
        self._client.headers["Cookie"] = self._cookie
        try:
            db.save_cookie(self._db_path, self._cookie)
        except Exception:  # 回写失败不影响当次播放
            log.exception("回写 __puus 失败")

    async def _request(self, method: str, path: str, ok_codes: frozenset[int] = frozenset(),
                       **kw) -> dict:
        """执行请求并校验业务 code。ok_codes 中的错误码不抛异常，
        由调用方通过返回值里的 "__code__" 区分（find_or_create_dir 用）。"""
        async with self._lock:
            # 瞬时 5xx（实测偶发）重试一次
            for attempt in (1, 2):
                resp = await self._client.request(method, BASE + path, **kw)
                self._absorb_puus(resp)
                if resp.status_code >= 500 and attempt == 1:
                    await asyncio.sleep(1)
                    continue
                break
            if resp.status_code in (401, 412):
                raise ProviderAuthError(f"登录态失效 (HTTP {resp.status_code})")
            try:
                data = resp.json()
            except ValueError:
                # 夸克业务错误可能带 HTTP 404/403（实测失效分享=404+code 41011），
                # 只有非 JSON 响应才按 HTTP 错误处理
                resp.raise_for_status()
                raise ProviderError(f"夸克接口非 JSON 响应 (HTTP {resp.status_code})")
        code = data.get("code")
        if code != 0:
            if code in AUTH_CODES:
                raise ProviderAuthError(f"登录态失效 (code={code}: {data.get('message')})")
            if code in ok_codes:
                d = dict(data.get("data") or {})
                d["__code__"] = code
                return d
            raise ProviderError(f"夸克接口错误 {path} code={code}: {data.get('message')}")
        return data.get("data") or {}

    # ---- 接口实现 ----

    async def verify(self) -> None:
        await self._request("GET", "/file/sort", params={
            "pdir_fid": "0", "_page": "1", "_size": "1", "_fetch_total": "1"})

    async def search_files(self, keyword: str, limit: int = 50) -> list[CloudFile]:
        data = await self._request("GET", "/file/search", params={
            "query": keyword, "_page": "1", "_size": str(min(limit, 100))})
        files = []
        for f in data.get("list", [])[:limit]:
            if f.get("dir"):
                continue  # 搜索契约：仅返回文件，不含目录
            files.append(CloudFile(
                fid=f["fid"], name=f["file_name"], size=int(f.get("size") or 0),
                is_dir=False, parent_path=f.get("parent_path", "")))
        return files

    async def get_play_resource(self, file: CloudFile) -> PlayResource:
        """获取播放直链。

        首选 /file/v2/play（网页播放器同源接口）：
          - 返回域名 video-play-c-*.drive.quark.cn 的播放直链，面向任意观看者分发，
            不绑定取链 IP（下载直链 dl-pc-* 实测对客户端直连限速严重）；
          - trans_status=raw 的条目即原画（video_info.size 与原文件一致），
            无需等待转码，未播放过的文件也有；
          - Range/206 正常，可拖动进度条。
        回退 /file/download 下载直链（该接口对大文件有 23018 大小限制，仅兜底小文件）。
        """
        try:
            data = await self._request("POST", "/file/v2/play", json={"fid": file.fid})
            entry = self._pick_play_entry(data)
            if entry:
                return PlayResource(
                    url=entry["video_info"]["url"],
                    headers={"Cookie": self._cookie, "Referer": REFERER, "User-Agent": USER_AGENT},
                    fid=file.fid,
                    filename=file.name,
                    video_size=file.size,
                    source_name=self.name,
                )
            log.warning("播放接口无可用条目，回退下载直链: %s", file.name)
        except ProviderError as e:
            log.warning("播放直链获取失败，回退下载直链: %s", e)

        data = await self._request("POST", "/file/download", json={"fids": [file.fid]})
        entries = data if isinstance(data, list) else data.get("data") or []
        if not entries or not entries[0].get("download_url"):
            raise ProviderError(f"未取得下载直链: {file.name}")
        return PlayResource(
            url=entries[0]["download_url"],
            headers={"Cookie": self._cookie, "Referer": REFERER, "User-Agent": USER_AGENT},
            fid=file.fid,
            filename=file.name,
            video_size=file.size,
            source_name=self.name,
        )

    @staticmethod
    def _pick_play_entry(data: dict) -> dict | None:
        """从 /file/v2/play 响应中选择最佳画质条目：原画优先，其次按分辨率档位。"""
        entries = [v for v in (data.get("video_list") or [])
                   if (v.get("video_info") or {}).get("url")
                   and v.get("accessable") is not False]
        if not entries:
            return None
        origin_size = int(data.get("size") or 0)
        for v in entries:
            vi = v["video_info"]
            if v.get("trans_status") == "raw" or \
               (origin_size and int(vi.get("size") or 0) == origin_size):
                return v
        return max(entries, key=lambda v: _RESOLUTION_RANK.get(v.get("resolution"), 0))

    async def close(self) -> None:
        await self._client.aclose()

    # ---- 分享转存（阶段 4；接口形态为实测探测结论，见 scripts/probe7_transfer.py）----

    async def get_share_stoken(self, pwd_id: str, passcode: str = "") -> str:
        try:
            data = await self._request("POST", "/share/sharepage/token",
                                       json={"pwd_id": pwd_id, "passcode": passcode})
        except ProviderError as e:
            # 41011=分享地址已失效；其余（如提取码错误）原样透出
            raise ProviderError(f"分享访问失败: {e}") from e
        stoken = data.get("stoken")
        if not stoken:
            raise ProviderError("分享响应缺少 stoken")
        return stoken

    async def list_share_files(self, pwd_id: str, stoken: str,
                               max_depth: int = 3) -> list[ShareFile]:
        """递归遍历分享目录树（每层分页拉全，深度与页数有上限防失控）。"""
        out: list[ShareFile] = []

        async def walk(pdir_fid: str, prefix: str, depth: int) -> None:
            page = 1
            while page <= 20:  # 单层最多 20 页 × 50 条
                data = await self._request("GET", "/share/sharepage/detail", params={
                    "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": pdir_fid,
                    "_page": str(page), "_size": "50", "_fetch_share": "1",
                    "_sort": "file_type:asc,file_name:asc"})
                lst = data.get("list") or []
                for e in lst:
                    entry = ShareFile(
                        fid=e.get("fid", ""), name=e.get("file_name", ""),
                        size=int(e.get("size") or 0), is_dir=bool(e.get("dir")),
                        fid_token=e.get("share_fid_token", ""),
                        path=f"{prefix}{e.get('file_name', '')}")
                    out.append(entry)
                    if entry.is_dir and depth < max_depth:
                        await walk(entry.fid, entry.path + "/", depth + 1)
                if len(lst) < 50:
                    return
                page += 1

        await walk("0", "", 0)
        return out

    async def save_share_files(self, pwd_id: str, stoken: str, items: list[ShareFile],
                               to_pdir_fid: str = "0") -> dict:
        """提交转存到 to_pdir_fid 目录。返回 {"task_id", "top_fids"}：
        top_fids 来自响应内嵌 task_resp（同步完成时直接给出落盘 fid，可能为空）。"""
        if not items:
            raise ProviderError("转存条目为空")
        data = await self._request("POST", "/share/sharepage/save", json={
            "fid_list": [i.fid for i in items],
            "fid_token_list": [i.fid_token for i in items],
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id, "stoken": stoken,
            "pdir_fid": "0", "scene": "link"})
        task_id = data.get("task_id") or ""
        if not task_id:
            raise ProviderError("转存响应缺少 task_id")
        embedded = ((data.get("task_resp") or {}).get("data") or {})
        top_fids = ((embedded.get("save_as") or {}).get("save_as_top_fids")) or []
        return {"task_id": task_id, "top_fids": [str(f) for f in top_fids]}

    async def get_transfer_task(self, task_id: str, retry_index: int = 0) -> dict:
        data = await self._request("GET", "/task", params={
            "task_id": task_id, "retry_index": str(retry_index)})
        save_as = data.get("save_as") or {}
        return {"status": int(data.get("status") or 0),
                "top_fids": [str(f) for f in (save_as.get("save_as_top_fids") or [])],
                "message": data.get("task_title", "")}

    async def find_or_create_dir(self, name: str) -> str:
        """根目录找同名文件夹（网盘搜索不返回目录，只能逐页 sort），无则创建。
        并发/重复创建报 23008 时再找一次（另一方可能已建好）。"""
        async def scan() -> str | None:
            page = 1
            while page <= 10:
                data = await self._request("GET", "/file/sort", params={
                    "pdir_fid": "0", "_page": str(page), "_size": "100"})
                lst = data.get("list") or []
                for e in lst:
                    if e.get("dir") and e.get("file_name") == name:
                        return e["fid"]
                if len(lst) < 100:
                    return None
                page += 1
            return None

        fid = await scan()
        if fid:
            return fid
        data = await self._request(
            "POST", "/file",
            json={"pdir_fid": "0", "file_name": name, "dir_path": "", "dir_init_lock": False},
            ok_codes=frozenset({23008}))
        if data.get("fid"):
            return data["fid"]
        fid = await scan()  # 23008：目录已存在（并发创建），重新扫描
        if fid:
            return fid
        raise ProviderError(f"目录创建冲突且无法定位: {name}")
