#!/usr/bin/env python3
"""阶段 1 POC：验证 夸克网盘文件 -> 可播放 URL -> Stremio 客户端播放 最小链路。

用法:
  export QUARK_COOKIE="从 pan.quark.cn 登录后复制的完整 Cookie"
  python poc_quark_verify.py verify                 # 验证 API 链路与直连可达性
  python poc_quark_verify.py verify --kw "电影名"    # 指定搜索关键词
  python poc_quark_verify.py serve                  # 起最小 Stremio Addon 供客户端实测

仅供本地调研使用；夸克接口为非官方私有接口。
"""
import argparse
import asyncio
import json
import os
import sys

import httpx

BASE = "https://drive-pc.quark.cn/1/clouddrive"
COMMON_PARAMS = {"pr": "ucpro", "fr": "pc"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 "
      "Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch")
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".ts", ".flv", ".webm", ".m2ts")

class QuarkClient:
    def __init__(self, cookie: str):
        self.cookie = cookie.strip()
        self.client = httpx.AsyncClient(
            timeout=30, follow_redirects=False,
            headers={"Cookie": self.cookie, "User-Agent": UA,
                     "Referer": "https://pan.quark.cn/",
                     "Content-Type": "application/json"},
            params=COMMON_PARAMS)

    async def _req(self, method: str, path: str, **kw) -> dict:
        r = await self.client.request(method, BASE + path, **kw)
        self._absorb_puus(r)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"quark api {path} code={data.get('code')}: {data.get('message')}")
        return data.get("data", {})

    def _absorb_puus(self, r: httpx.Response) -> None:
        puus = r.cookies.get("__puus")
        if puus:
            self.cookie = self.cookie.split("__puus=")[0].rstrip("; ") + f"; __puus={puus}"
            self.client.headers["Cookie"] = self.cookie

    async def list_dir(self, pdir_fid: str = "0", page: int = 1, size: int = 100) -> dict:
        return await self._req("GET", "/file/sort", params={
            "pdir_fid": pdir_fid, "_page": str(page), "_size": str(size),
            "_fetch_total": "1", "_sort": "file_type:asc,updated_at:desc"})

    async def search(self, keyword: str) -> list:
        d = await self._req("GET", "/file/search", params={"query": keyword})
        return d.get("list", [])

    async def download_url(self, fid: str) -> str:
        d = await self._req("POST", "/file/download", json={"fids": [fid]})
        return d[0]["download_url"]

    async def hls_play_url(self, fid: str) -> str:
        d = await self._req("POST", "/file/v2/play/project", json={
            "fid": fid, "resolutions": "low,normal,high,super,2k,4k",
            "supports": "fmp4_av,m3u8"})
        for v in d.get("video_list", []):
            u = (v.get("video_info") or {}).get("url", "")
            if u:
                return u
        return ""

    async def close(self):
        await self.client.aclose()

async def probe_url(client: httpx.AsyncClient, url: str, with_headers: dict | None) -> dict:
    res = {"reachable": False, "range_206": False, "redirect": None,
           "content_type": None, "content_length": None}
    try:
        h = dict(with_headers or {})
        h.setdefault("Range", "bytes=0-1023")
        r = await client.get(url, headers=h)
        res["reachable"] = r.status_code in (200, 206)
        res["range_206"] = r.status_code == 206
        res["content_type"] = r.headers.get("content-type")
        clen = r.headers.get("content-length")
        res["content_length"] = int(clen) if clen and clen.isdigit() else None
        if r.status_code // 100 == 3:
            res["redirect"] = r.headers.get("location", "")
    except Exception as e:
        res["error"] = repr(e)
    return res

async def cmd_verify(keyword: str) -> int:
    cookie = os.environ.get("QUARK_COOKIE", "")
    if not cookie:
        print("❌ 未设置 QUARK_COOKIE 环境变量"); return 1
    q = QuarkClient(cookie)
    report = {}
    try:
        root = await q.list_dir("0")
        names = [f["file_name"] for f in root.get("list", [])][:5]
        print(f"✅ 登录态有效，根目录样例: {names}")
        report["login"] = True

        files = await q.search(keyword)
        videos = [f for f in files
                  if not f.get("dir") and f.get("file_name", "").lower().endswith(VIDEO_EXT)]
        print(f"✅ 搜索 '{keyword}' 共 {len(files)} 项，其中视频 {len(videos)} 个")
        report["search_hits"] = len(videos)
        if not videos:
            print("⚠️ 未找到视频，请向网盘放入一个合法测试视频后重试"); return 2

        f = videos[0]
        print(f"🎯 目标: {f['file_name']} size={f.get('size')} fid={f['fid']}")

        direct = await q.download_url(f["fid"])
        print(f"✅ 原画直链: {direct[:100]}...")
        report["direct"] = await probe_url(httpx.AsyncClient(follow_redirects=True), direct, None)
        print(f"   无头直连: {report['direct']}")
        report["direct_with_cookie"] = await probe_url(
            httpx.AsyncClient(follow_redirects=True),
            direct, {"Cookie": q.cookie, "Referer": "https://pan.quark.cn/", "User-Agent": UA})
        print(f"   带 Cookie 直连: {report['direct_with_cookie']}")

        hls = None
        try:
            hls = await q.hls_play_url(f["fid"])
            report["hls"] = {"url": hls[:100] if hls else None}
            if hls:
                report["hls"] = await probe_url(httpx.AsyncClient(follow_redirects=True), hls, None)
                print(f"✅ 转码 HLS: {hls[:100]}... -> {report['hls']}")
        except Exception as e:
            report["hls"] = {"error": repr(e)}
            print(f"⚠️ 转码 HLS 端点不可用（记为已知问题）: {e}")

        state = {
            "url": hls if hls and report.get("hls", {}).get("reachable") else direct,
            "direct_url": direct,
            "hls_url": hls,
            "fid": f["fid"],
            "headers": {"Cookie": q.cookie, "Referer": "https://pan.quark.cn/", "User-Agent": UA},
            "file_name": f["file_name"],
            "size": f.get("size"),
        }
        with open("/tmp/poc_stream.json", "w") as fp:
            json.dump(state, fp, ensure_ascii=False)
        print("📁 已将播放状态写入 /tmp/poc_stream.json，供 serve 子命令使用")
    finally:
        await q.close()

    ok = report.get("direct_with_cookie", {}).get("range_206") or \
         report.get("hls", {}).get("reachable")
    print(f"\n=== 链路验证 {'通过 ✅' if ok else '存在风险 ⚠️'} ===")
    print("下一步: python poc_quark_verify.py serve 后按 README 在 Stremio 客户端实测播放")
    return 0 if ok else 3

def cmd_serve(port: int, host: str) -> int:
    state_file = "/tmp/poc_stream.json"
    if not os.path.exists(state_file):
        print("❌ 请先运行 verify 生成播放状态"); return 1
    with open(state_file) as fp:
        state = json.load(fp)

    q = QuarkClient(state["headers"]["Cookie"])

    async def fresh_url() -> tuple[str, dict]:
        """每次请求实时获取新直链，避免签名 URL 过期；失败时回退缓存。"""
        try:
            url = await q.download_url(state["fid"])
            return url, {"Cookie": q.cookie, "Referer": "https://pan.quark.cn/", "User-Agent": UA}
        except Exception as e:
            print(f"⚠️ 实时取直链失败，使用缓存: {e}")
            return state["url"], state["headers"]

    import uvicorn
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])
    manifest = {
        "id": "community.poc.quark", "version": "0.0.1", "name": "Quark POC",
        "description": "阶段1最小链路验证: 个人网盘文件->可播放URL",
        "resources": ["stream"], "types": ["movie"], "catalogs": [],
        "idPrefixes": ["tt"], "behaviorHints": {"configurable": False},
    }

    @app.get("/manifest.json")
    async def get_manifest():
        return manifest

    @app.get("/stream/movie/{vid}.json")
    async def get_stream(vid: str):
        play_url, headers = await fresh_url()
        is_hls = ".m3u8" in play_url
        hints = {"notWebReady": not is_hls, "bingeGroup": "quarkpoc",
                 "filename": state["file_name"], "videoSize": state["size"]}
        if not is_hls:
            hints["proxyHeaders"] = {"request": headers}
        return {"streams": [{
            "url": play_url,
            "name": "夸克POC\nHLS" if is_hls else "夸克POC\n直链",
            "title": f"{state['file_name']} ({vid})",
            "description": "带请求头直链，桌面端经内置服务器带头转发" if not is_hls else "转码HLS直连",
            "behaviorHints": hints,
        }]}

    print(f"Min Stremio addon: http://{host}:{port}/manifest.json")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["verify", "serve"])
    ap.add_argument("--kw", default="test", help="网盘内搜索关键词")
    ap.add_argument("--host", default="0.0.0.0", help="serve 监听地址")
    ap.add_argument("--port", type=int, default=7001, help="serve 监听端口")
    args = ap.parse_args()
    if args.cmd == "verify":
        sys.exit(asyncio.run(cmd_verify(args.kw)))
    sys.exit(cmd_serve(args.port, args.host))

if __name__ == "__main__":
    main()