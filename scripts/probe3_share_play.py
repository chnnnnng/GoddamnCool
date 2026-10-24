"""第三轮探测：play 接口分辨率取值变体 + 分享链路取直链（大文件）。

要回答的问题：
1. play/project 换分辨率取值（240/360/...、sd/hd/shd/ud、singular）能否 code=0？
2. 分享链路（share/create -> sharepage/token -> sharepage/download）
   对 871MB 大文件能出什么域名的直链？
3. 该直链无 Cookie 能否下载（决定 Stremio 客户端直连是否可行）？
4. 删除分享后直链是否失效（决定分享是否需要长期保留）？
"""
import asyncio
import json
import os
import sys
import time
from urllib.parse import urlparse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.play_url_probe import BASE, api_headers, speed, walk_urls  # noqa: E402

BIG_FID = "e0bb2fd09dd044f8ac0dac383ee64778"


def host(u: str) -> str:
    return urlparse(u).netloc


async def main():
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        # ---- 1. play/project 分辨率取值变体（大文件）----
        play_bodies = [
            {"fids": [BIG_FID], "resolutions": ["240", "360", "480", "720", "1080"]},
            {"fids": [BIG_FID], "resolutions": ["sd", "hd", "shd", "ud"]},
            {"fids": [BIG_FID], "resolutions": ["SD", "HD", "SHD", "UD"]},
            {"fids": [BIG_FID], "resolution": "super"},
            {"fids": [BIG_FID], "resolutions": ["low", "normal", "high", "super", "2k", "4k"]},
        ]
        for i, payload in enumerate(play_bodies):
            r = await c.post(f"{BASE}/file/v2/play/project",
                             params={"pr": "ucpro", "fr": "pc"}, json=payload,
                             headers=api_headers())
            b = r.json()
            print(f"[play #{i}] {json.dumps(payload, ensure_ascii=False)[:90]} "
                  f"-> code={b.get('code')} msg={b.get('message')}")
            if b.get("code") == 0:
                print(f"    data: {json.dumps(b.get('data'), ensure_ascii=False)[:800]}")

        # POST /file/v2/play 试探
        r = await c.post(f"{BASE}/file/v2/play", params={"pr": "ucpro", "fr": "pc"},
                         json={"fid": BIG_FID}, headers=api_headers())
        print(f"[POST /file/v2/play] HTTP {r.status_code} -> {r.text[:200]}")

        # ---- 2. 分享链路取直链 ----
        print("\n----- 分享链路 -----")
        r = await c.post(f"{BASE}/share/create", params={"pr": "ucpro", "fr": "pc"},
                         json={"fid_list": [BIG_FID], "title": "link-test",
                               "url_type": 1, "expired_type": 1},
                         headers=api_headers())
        b = r.json()
        print(f"[share/create] code={b.get('code')} msg={b.get('message')} "
              f"data={json.dumps(b.get('data'), ensure_ascii=False)[:300]}")
        if b.get("code") != 0:
            return
        d = b["data"]
        share_id, passcode = d.get("share_id"), d.get("passcode", "")

        r = await c.post(f"{BASE}/share/sharepage/token", params={"pr": "ucpro", "fr": "pc"},
                         json={"pwd_id": share_id, "passcode": passcode},
                         headers=api_headers())
        b = r.json()
        stoken = (b.get("data") or {}).get("stoken")
        print(f"[sharepage/token] code={b.get('code')} stoken={'有' if stoken else '无'}")
        if not stoken:
            return

        r = await c.post(f"{BASE}/share/sharepage/download",
                         params={"pr": "ucpro", "fr": "pc"},
                         json={"pwd_id": share_id, "stoken": stoken, "fids": [BIG_FID]},
                         headers=api_headers())
        b = r.json()
        print(f"[sharepage/download] code={b.get('code')} msg={b.get('message')}")
        urls = []
        walk_urls(b.get("data") or {}, urls)
        dl_url = None
        for k, u in urls:
            if "download_url" in k and u:
                dl_url = u
                print(f"  [{k}] {host(u)}")
                print(f"  URL: {u[:160]}")
        if not dl_url:
            print(f"  data: {json.dumps(b.get('data'), ensure_ascii=False)[:600]}")
            return

        # ---- 3. 速度与无头访问 ----
        print("\n----- 分享直链测速（服务器侧）-----")
        s1 = await speed(dl_url, with_cookie=True)
        print(f"带 Cookie: {s1}")
        s2 = await speed(dl_url, with_cookie=False)
        print(f"无 Cookie: {s2}")

        # ---- 4. 删除分享后再测 ----
        r = await c.post(f"{BASE}/share/delete", params={"pr": "ucpro", "fr": "pc"},
                         json={"share_ids": [share_id], "exclude_fids": []},
                         headers=api_headers())
        b = r.json()
        print(f"\n[share/delete] code={b.get('code')} msg={b.get('message')}")
        s3 = await speed(dl_url, with_cookie=False)
        print(f"删除分享后无 Cookie 复测: {s3}")

        with open("/tmp/share_probe.json", "w") as f:
            json.dump({"share_id": share_id, "deleted": True, "url": dl_url,
                       "speed_cookie": s1, "speed_headless": s2,
                       "speed_after_delete": s3}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
