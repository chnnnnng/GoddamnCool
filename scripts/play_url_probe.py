"""探测夸克"网页播放直链"（video-play-c-*.drive.quark.cn 域名）的 API 来源。

背景：
- 我们当前用 POST /file/download 取直链，域名是 dl-pc-sz.drive.quark.cn，客户端直连慢。
- 用户在夸克网页端 DevTools 看到的播放域名是 video-play-c-sz.drive.quark.cn。
- 假设：网页播放器走的是 play API（如 /file/v2/play/project），其返回的
  播放域名面向任意观看者，不做取链 IP 绑定 / 不受下载直链 QoS 限制。

本脚本枚举 play/download API 的多种参数变体，打印返回的 URL 域名，
并对每种域名在服务器侧测速（带 Cookie 与无头各测一次，仅作参考）。
结果同时写入 /tmp/play_probe.json。
"""
import asyncio
import json
import os
import sys
import time
from urllib.parse import urlparse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "https://drive-pc.quark.cn/1/clouddrive"
FID = "e0bb2fd09dd044f8ac0dac383ee64778"  # 余罪.S01E12 测试文件
PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
REFERER = "https://pan.quark.cn/"


def load_cookie() -> str:
    for src in (os.environ.get("QUARK_COOKIE"), "/root/workspace/QUARK_COOKIE"):
        if src and os.path.isfile(str(src)):
            return open(src).read().strip()
        if src:
            return src.strip()
    raise SystemExit("未找到 Cookie（QUARK_COOKIE 环境变量或 /root/workspace/QUARK_COOKIE）")


COOKIE = load_cookie()


def api_headers(ua: str = PC_UA) -> dict:
    return {"Cookie": COOKIE, "Referer": REFERER, "User-Agent": ua,
            "Content-Type": "application/json"}


def walk_urls(obj, out: list, prefix: str = ""):
    """递归提取 JSON 里所有 http(s) 字符串。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk_urls(v, out, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk_urls(v, out, f"{prefix}[{i}]")
    elif isinstance(obj, str) and obj.startswith("http"):
        out.append((prefix, obj))


async def call_api(client, method, path, params, payload=None, label=""):
    url = BASE + path
    try:
        r = await client.request(method, url, params=params, json=payload,
                                 headers=api_headers())
    except Exception as ex:
        print(f"[{label}] 请求异常: {ex}")
        return None
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:200]}
    code, msg = body.get("code"), body.get("message")
    print(f"[{label}] {method} {path} {' '.join(f'{k}={v}' for k, v in (params or {}).items())}"
          f" -> HTTP {r.status_code} code={code} msg={msg}")
    if code == 0 and isinstance(body.get("data"), dict):
        data = body["data"]
        print(f"    data keys: {list(data.keys())}")
        print(f"    data: {json.dumps(data, ensure_ascii=False)[:1200]}")
    return body


async def speed(url: str, with_cookie: bool, seconds: float = 4.0) -> str:
    headers = ({"Cookie": COOKIE, "Referer": REFERER, "User-Agent": PC_UA}
               if with_cookie else
               {"User-Agent": PC_UA, "Referer": "https://pan.quark.cn/"})
    got, t0 = 0, None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5),
                                     follow_redirects=True, headers=headers) as c:
            async with c.stream("GET", url) as r:
                ctype = r.headers.get("content-type", "")
                t0 = time.monotonic()
                async for chunk in r.aiter_bytes(65536):
                    got += len(chunk)
                    if time.monotonic() - t0 > seconds:
                        break
                if "mpegurl" in ctype or url.split("?")[0].endswith(".m3u8"):
                    return f"m3u8 可访问(HTTP {r.status_code})"
    except Exception as ex:
        return f"err: {type(ex).__name__}"
    if not t0 or not got:
        return "无数据"
    dt = time.monotonic() - t0
    return f"{got / 1024 / dt:.0f} KB/s"


async def main():
    results = []  # (label, [(key, url)])
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        # ---- A: 下载接口基线 ----
        for fr in ("pc", "h5"):
            body = await call_api(c, "POST", "/file/download",
                                  {"pr": "ucpro", "fr": fr}, {"fids": [FID]},
                                  label=f"A-download fr={fr}")
            urls = []
            if body:
                walk_urls(body.get("data") or {}, urls)
            results.append((f"A file/download fr={fr}", urls))

        # ---- B: 播放接口 play/project 参数变体 ----
        queries = [
            {"pr": "ucpro", "fr": "pc"},
            {"pr": "ucpro", "fr": "pc", "platform": "pc"},
            {"pr": "ucpro", "fr": "pc", "platform": "web"},
            {"pr": "ucpro", "fr": "h5", "platform": "h5"},
            {"pr": "ucpro", "fr": "h5"},
        ]
        payloads = [
            {"fids": [FID]},
            {"fids": [FID], "resolutions": ["low", "high", "super", "2k", "4k"]},
        ]
        for qi, q in enumerate(queries):
            for pi, p in enumerate(payloads):
                label = f"B{qi}{pi} play/project q={'+'.join(q.values())} p={pi}"
                body = await call_api(c, "POST", "/file/v2/play/project", q, p, label=label)
                urls = []
                if body:
                    walk_urls(body.get("data") or {}, urls)
                results.append((label, urls))

        # ---- C: 单文件播放接口猜测 ----
        body = await call_api(c, "GET", "/file/v2/play",
                              {"pr": "ucpro", "fr": "pc", "fid": FID}, label="C play GET")
        urls = []
        if body:
            walk_urls(body.get("data") or {}, urls)
        results.append(("C file/v2/play GET", urls))

    # ---- 汇总：按域名去重测速 ----
    print("\n===== URL 域名汇总 =====")
    seen_hosts = {}
    for label, urls in results:
        for key, u in urls:
            h = urlparse(u).netloc
            print(f"{label} [{key}] -> {h}  {u[:100]}...")
            seen_hosts.setdefault(h, (label, key, u))

    print("\n===== 速度测试（服务器侧，仅供参考）=====")
    summary = []
    for h, (label, key, u) in seen_hosts.items():
        s1 = await speed(u, with_cookie=True)
        s2 = await speed(u, with_cookie=False)
        print(f"{h}\n  来源: {label} [{key}]\n  带Cookie: {s1}\n  无Cookie: {s2}")
        summary.append({"host": h, "label": label, "key": key,
                        "url": u, "speed_cookie": s1, "speed_headless": s2})

    with open("/tmp/play_probe.json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("\n已写入 /tmp/play_probe.json")


if __name__ == "__main__":
    asyncio.run(main())
