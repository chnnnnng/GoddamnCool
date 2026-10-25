"""夸克网盘 Provider。

接口为非官方私有接口（阶段 1 POC 已验证），随夸克版本可能变更；
所有端点细节集中在本文件，便于后续适配。基址与公共参数：
  https://drive-pc.quark.cn/1/clouddrive?pr=ucpro&fr=pc
鉴权：请求头 Cookie（网页版凭证），响应 Set-Cookie 中的 __puus 必须回写。
"""
import asyncio
import base64
import hashlib
import json
import logging
import time

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

    def __init__(self, cookie: str, db_path: str, user_id: str = ""):
        self._cookie = cookie.strip()
        self._db_path = db_path
        # 绑定用户：响应刷新 __puus 时回写该用户的加密凭证（多用户隔离）
        self._user_id = user_id
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
        """响应携带新 __puus 时回写内存与数据库（限流对抗，关键工程细节）。

        仅在构造时传入 user_id（绑定用户）才落库，避免误写他人凭证。"""
        puus = resp.cookies.get("__puus")
        if not puus:
            return
        head = self._cookie.split("__puus=")[0].rstrip("; ")
        self._cookie = (head + ";" if head else "") + f"__puus={puus}"
        self._client.headers["Cookie"] = self._cookie
        if not self._user_id:
            return
        try:
            db.save_user_cookie(self._db_path, self._user_id, self._cookie)
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
                # play API 返回的 data.size 即原文件大小，优先用它
                # （file_index / transfer_task 路径构造的 CloudFile size 可能为 0）
                video_size = int(data.get("size") or 0) or file.size
                return PlayResource(
                    url=entry["video_info"]["url"],
                    headers={"Cookie": self._cookie, "Referer": REFERER, "User-Agent": USER_AGENT},
                    fid=file.fid,
                    filename=file.name,
                    video_size=video_size,
                    source_name=self.name,
                )
            log.warning("播放接口无可用条目，回退下载直链: %s", file.name)
        except ProviderError as e:
            log.warning("播放直链获取失败，回退下载直链: %s", e)

        data = await self._request("POST", "/file/download", json={"fids": [file.fid]})
        entries = data if isinstance(data, list) else data.get("data") or []
        if not entries or not entries[0].get("download_url"):
            raise ProviderError(f"未取得下载直链: {file.name}")
        dl_size = int(entries[0].get("size") or 0) or file.size
        return PlayResource(
            url=entries[0]["download_url"],
            headers={"Cookie": self._cookie, "Referer": REFERER, "User-Agent": USER_AGENT},
            fid=file.fid,
            filename=file.name,
            video_size=dl_size,
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

    async def list_all_file_names(self, max_depth: int = 3) -> set[str]:
        """递归列出网盘全部文件名（用于"分享文件网盘中已存在"提示比对）。

        搜索接口不返回目录，故用 /file/sort 逐层分页；深度与页数有上限防失控。
        注意：/file/sort 单页实际上限约 50（传 100 会被截断），分页结束
        优先用响应 total 判断，缺失时退回 len < size。"""
        names: set[str] = set()
        size = 50

        async def walk(pdir_fid: str, depth: int) -> None:
            page = 1
            while page <= 50:  # 单层最多 50 页 × 50 条
                data = await self._request("GET", "/file/sort", params={
                    "pdir_fid": pdir_fid, "_page": str(page),
                    "_size": str(size), "_fetch_total": "1"})
                lst = data.get("list") or []
                for e in lst:
                    if e.get("dir"):
                        if depth < max_depth:
                            await walk(e.get("fid", ""), depth + 1)
                    elif e.get("file_name"):
                        names.add(e["file_name"])
                total = data.get("_total") or data.get("total")
                if total is not None:
                    if page * size >= int(total):
                        return
                elif len(lst) < size:
                    return
                page += 1

        await walk("0", 0)
        return names

    async def list_dir_files(self, pdir_fid: str = "0", *, max_depth: int = 2,
                             limit: int = 2000) -> list[CloudFile]:
        """递归列出某目录下的文件（含大小与 fid），供转存后智能重命名定位。"""
        out: list[CloudFile] = []
        size = 50

        async def walk(fid: str, depth: int) -> None:
            page = 1
            while page <= 50 and len(out) < limit:
                data = await self._request("GET", "/file/sort", params={
                    "pdir_fid": fid, "_page": str(page),
                    "_size": str(size), "_fetch_total": "1"})
                lst = data.get("list") or []
                for e in lst:
                    if len(out) >= limit:
                        return
                    if e.get("dir"):
                        if depth < max_depth:
                            await walk(e.get("fid", ""), depth + 1)
                    elif e.get("file_name"):
                        out.append(CloudFile(
                            fid=str(e.get("fid") or ""),
                            name=e["file_name"],
                            size=int(e.get("size") or 0),
                            is_dir=False,
                            parent_path=""))
                total = data.get("_total") or data.get("total")
                if total is not None:
                    if page * size >= int(total):
                        return
                elif len(lst) < size:
                    return
                page += 1

        await walk(pdir_fid or "0", 0)
        return out

    async def list_all_files(self, max_depth: int = 3) -> list[CloudFile]:
        """递归列出网盘全部文件（含大小），供"精确大小+粗略片名"已有匹配。"""
        return await self.list_dir_files("0", max_depth=max_depth, limit=5000)

    async def rename_file(self, fid: str, new_name: str) -> None:
        """重命名网盘文件（转存后智能重命名用，接口同开源实现 quark-auto-save）。"""
        await self._request("POST", "/file/rename",
                            json={"fid": fid, "file_name": new_name})

    async def delete_files(self, fids: list[str]) -> None:
        """删除网盘文件（回收站，可用于清理旧的记录文件）。"""
        if not fids:
            return
        await self._request("POST", "/file/delete", json={
            "action_type": 2, "filelist": fids, "exclude_fids": []})

    async def download_text(self, fid: str, *, max_bytes: int = 1 << 20) -> str:
        """下载小文本文件内容（读取已有重命名记录用）。失败抛 ProviderError。"""
        data = await self._request("POST", "/file/download", json={"fids": [fid]})
        entries = data if isinstance(data, list) else (data.get("data") or [])
        url = (entries[0] or {}).get("download_url") if entries else ""
        if not url:
            raise ProviderError("未取得下载直链")
        # 下载直链会 302 跳转，client 配置了 follow_redirects=False，手动跟随
        try:
            async with self._lock:
                resp = await self._client.get(url)
                for _ in range(5):
                    if resp.status_code not in (301, 302, 303, 307, 308):
                        break
                    loc = resp.headers.get("location") or ""
                    if not loc:
                        break
                    resp = await self._client.get(loc)
                resp.raise_for_status()
                return resp.content[:max_bytes].decode("utf-8", "replace")
        except httpx.HTTPError as e:
            raise ProviderError(f"下载文本文件失败: {e}") from e

    async def upload_text(self, pdir_fid: str, name: str, text: str) -> None:
        """上传小文本文件（单分片 ≤4MB，用于写入智能重命名记录）。

        流程对齐开源实现（alist quark 驱动）：/file/upload/pre →
        /file/upload/auth → OSS PUT 单分片 → /file/upload/auth(提交) →
        OSS POST complete → /file/upload/finish。任一步失败抛 ProviderError。"""
        content = text.encode("utf-8")
        if len(content) > 4 << 20:
            raise ProviderError("文本文件过大（>4MB）")
        now_ms = int(time.time() * 1000)
        pre = await self._request("POST", "/file/upload/pre", json={
            "ccp_hash_update": True, "dir_name": "", "file_name": name,
            "format_type": "text/plain", "l_created_at": now_ms,
            "l_updated_at": now_ms, "pdir_fid": pdir_fid or "0",
            "size": len(content)})
        if pre.get("finish"):  # 秒传命中（同一内容此前上传过）
            return
        bucket, obj_key = str(pre.get("bucket") or ""), str(pre.get("obj_key") or "")
        upload_id, task_id = str(pre.get("upload_id") or ""), str(pre.get("task_id") or "")
        host = str(pre.get("upload_url") or "").removeprefix("https://")
        auth_info = pre.get("auth_info")
        if not (bucket and obj_key and upload_id and host):
            raise ProviderError("上传 pre 响应缺少必要字段")
        mime, ua = "text/plain", \
            "aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit"

        def httpdate() -> str:
            return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())

        url = f"https://{bucket}.{host}/{obj_key}"
        # 分片 PUT 授权
        d = httpdate()
        auth = await self._request("POST", "/file/upload/auth", json={
            "auth_info": auth_info,
            "auth_meta": (f"PUT\n{mime}\n{d}\nx-oss-date:{d}\n"
                          f"x-oss-user-agent:{ua}\n"
                          f"/{bucket}/{obj_key}?partNumber=1&uploadId={upload_id}"),
            "task_id": task_id})
        auth_key = str(auth.get("auth_key") or "")
        if not auth_key:
            raise ProviderError("上传 auth 响应缺少 auth_key")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.put(
                    url, params={"partNumber": "1", "uploadId": upload_id},
                    content=content,
                    headers={"Authorization": auth_key, "Content-Type": mime,
                             "Referer": REFERER, "x-oss-date": d,
                             "x-oss-user-agent": ua})
                if r.status_code != 200:
                    raise ProviderError(f"分片上传失败 (HTTP {r.status_code})")
                etag = (r.headers.get("etag") or "").strip('"')
                # 提交分片（complete multipart upload）
                xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                       "<CompleteMultipartUpload>\n"
                       f"<Part><PartNumber>1</PartNumber><ETag>{etag}</ETag></Part>\n"
                       "</CompleteMultipartUpload>")
                md5_b64 = base64.b64encode(
                    hashlib.md5(xml.encode()).digest()).decode()
                callback_b64 = ""
                if pre.get("callback"):
                    callback_b64 = base64.b64encode(
                        json.dumps(pre["callback"]).encode("utf-8")).decode()
                d2 = httpdate()
                auth2 = await self._request("POST", "/file/upload/auth", json={
                    "auth_info": auth_info,
                    "auth_meta": (
                        f"POST\n{md5_b64}\napplication/xml\n{d2}\n"
                        + (f"x-oss-callback:{callback_b64}\n" if callback_b64 else "")
                        + f"x-oss-date:{d2}\nx-oss-user-agent:{ua}\n"
                        f"/{bucket}/{obj_key}?uploadId={upload_id}"),
                    "task_id": task_id})
                auth_key2 = str(auth2.get("auth_key") or "")
                if not auth_key2:
                    raise ProviderError("提交上传 auth 响应缺少 auth_key")
                headers2 = {"Authorization": auth_key2, "Content-MD5": md5_b64,
                            "Content-Type": "application/xml", "Referer": REFERER,
                            "x-oss-date": d2, "x-oss-user-agent": ua}
                if callback_b64:
                    headers2["x-oss-callback"] = callback_b64
                r2 = await client.post(url, params={"uploadId": upload_id},
                                       content=xml.encode(), headers=headers2)
                if r2.status_code != 200:
                    raise ProviderError(f"提交分片失败 (HTTP {r2.status_code})")
        except httpx.HTTPError as e:
            raise ProviderError(f"OSS 上传失败: {e}") from e
        await self._request("POST", "/file/upload/finish",
                            json={"obj_key": obj_key, "task_id": task_id})

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
