"""PanSou 聚合搜索 Provider（github.com/fish2018/pansou，MIT 协议）。

调用其公开搜索 API：
  POST {base_url}/api/search
  body: {"kw": "...", "res": "merge", "cloud_types": ["quark"]}
返回 data.merged_by_type.<disk_type>[]，每项含 url/password/note/datetime/source。

实测要点（2026-10，公开实例 so.252035.xyz）：
- GET 会被实例 nginx 302 后拒绝（400），必须用 POST；
- 实例边缘节点不稳定：同一请求可能瞬时 400/502/ReadTimeout，需重试；
- 插件异步搜索：关键词首次查询常返回 0 条，稍候重查即可命中其缓存，
  故空结果时自动补查一次；
- 夸克链接做严格格式校验：https://pan.quark.cn/s/<10~20位小写字母数字>
  （个别插件返回脏链接，share id 后拼接多余字符）；非夸克网盘仅做宽松
  http(s) URL 校验（不可转存，仅展示/跳转）；
- datetime 可能为 Go 零值 "0001-01-01T00:00:00Z"，视为未知。
"""
import asyncio
import logging
import re

import httpx

from .base import ResourceSearchProvider, SearchCandidate, SearchError

log = logging.getLogger(__name__)

# 夸克分享链接格式（share id 实测为小写字母数字；含大写即视为脏数据）
SHARE_URL_RE = re.compile(r"^https://pan\.quark\.cn/s/[0-9a-z]{10,20}$")
# 非夸克网盘的宽松校验：仅要求 http(s) 链接（用于独立搜索的多网盘展示）
LOOSE_URL_RE = re.compile(r"^https?://\S+$")

_ZERO_TIME = "0001-01-01T00:00:00Z"


class PanSouProvider(ResourceSearchProvider):
    name = "PanSou"

    def __init__(self, base_url: str, timeout: float = 20.0, retry_backoff: float = 1.0):
        self._base = base_url.rstrip("/")
        self._backoff = retry_backoff
        origin = self._base
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10),
            follow_redirects=False,
            # 公开实例位于 Cloudflare 后，裸 API 指纹会被反爬拦截
            # （实测返回 error 1003），需要浏览器样请求头。
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/126.0.0.0 Safari/537.36",
                "Referer": origin + "/",
                "Origin": origin,
            },
        )

    async def search(self, keyword: str, limit: int = 20,
                     cloud_types: list[str] | None = None) -> list[SearchCandidate]:
        body = {"kw": keyword, "res": "merge"}
        if cloud_types is None:
            cloud_types = ["quark"]  # 默认仅搜夸克（可转存链路）
        if cloud_types:
            body["cloud_types"] = cloud_types
        last_err = ""
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(self._backoff * attempt)
            try:
                resp = await self._client.post(f"{self._base}/api/search", json=body)
            except httpx.HTTPError as e:
                last_err = f"PanSou 请求失败: {e}"
                log.warning("PanSou 第 %d 次请求失败: %s", attempt + 1, e)
                continue  # 实例瞬时 400/502/超时，重试
            if resp.status_code != 200:
                last_err = f"PanSou HTTP {resp.status_code}"
                log.warning("PanSou 第 %d 次请求 HTTP %d", attempt + 1, resp.status_code)
                continue
            try:
                data = resp.json()
            except ValueError as e:
                last_err = f"PanSou 响应非 JSON: {e}"
                continue
            if data.get("code") != 0:
                last_err = f"PanSou 错误 code={data.get('code')}: {data.get('message')}"
                continue

            merged = ((data.get("data") or {}).get("merged_by_type") or {})
            items: list[tuple[str, dict]] = []
            for disk_type, arr in merged.items():
                if cloud_types and disk_type not in cloud_types:
                    continue
                for it in arr or []:
                    items.append((disk_type, it))
            out = self._parse(items)
            if not out and attempt == 0:
                # 插件异步搜索：首次常为空，稍候重查命中缓存
                log.info("PanSou 首查无结果（%r），等待缓存后补查", keyword)
                await asyncio.sleep(1.5)
                continue
            log.info("PanSou 搜索 %r（网盘 %s）: 原始 %d 条，有效 %d 条",
                     keyword, ",".join(cloud_types) or "全部", len(items), len(out))
            return out[:limit]
        raise SearchError(last_err or "PanSou 无结果")

    async def check_links(self, items: list[dict]) -> list[dict]:
        """批量检测分享链接存活状态（POST /api/check/links）。

        items: [{"disk_type": "quark", "url": ..., "password": ...?}]
        返回 PanSou results 原始条目（state: ok/bad/locked/unsupported/uncertain，
        附 summary/cache_hit 等）。检测由 PanSou 服务端代查并自带缓存
        （cache_hit/expires_at），调用方无需再做本地缓存；整体失败抛 SearchError。
        """
        last_err = ""
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(self._backoff * attempt)
            try:
                resp = await self._client.post(
                    f"{self._base}/api/check/links", json={"items": items})
            except httpx.HTTPError as e:
                last_err = f"PanSou 检测请求失败: {e}"
                log.warning("PanSou 检测第 %d 次请求失败: %s", attempt + 1, e)
                continue
            if resp.status_code != 200:
                last_err = f"PanSou 检测 HTTP {resp.status_code}"
                log.warning("PanSou 检测第 %d 次 HTTP %d", attempt + 1, resp.status_code)
                continue
            try:
                data = resp.json()
            except ValueError as e:
                last_err = f"PanSou 检测响应非 JSON: {e}"
                continue
            results = data.get("results")
            if not isinstance(results, list):
                last_err = f"PanSou 检测响应缺少 results: {data.get('message') or data}"
                continue
            log.info("PanSou 检测 %d 条链接: %s",
                     len(results), [r.get("state") for r in results])
            return results
        raise SearchError(last_err or "PanSou 检测失败")

    def _parse(self, items: list[tuple[str, dict]]) -> list[SearchCandidate]:
        out: list[SearchCandidate] = []
        seen: set[str] = set()
        for disk_type, it in items:
            url = (it.get("url") or "").strip()
            if url in seen:
                continue  # 同链接去重
            if disk_type == "quark" or "pan.quark.cn/s/" in url:
                if not SHARE_URL_RE.match(url):
                    continue  # 夸克：脏链接（拼接错误/非官方域）过滤
                disk = "quark"
            else:
                if not LOOSE_URL_RE.match(url):
                    continue  # 非夸克：仅宽松校验 http(s) 链接
                disk = disk_type
            seen.add(url)
            published = it.get("datetime") or ""
            out.append(SearchCandidate(
                title=(it.get("note") or "").strip(),
                share_url=url,
                passcode=(it.get("password") or "").strip(),
                source=(it.get("source") or "").strip(),
                published="" if published == _ZERO_TIME else published,
                provider=self.name,
                disk=disk,
            ))
        return out

    async def close(self) -> None:
        await self._client.aclose()
