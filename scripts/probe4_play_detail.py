"""第四轮：完整解析 /file/v2/play 响应，提取播放直链并测速。"""
import asyncio
import json
import sys
from urllib.parse import urlparse

import httpx

import os  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.play_url_probe import BASE, api_headers, speed  # noqa: E402

BIG_FID = "e0bb2fd09dd044f8ac0dac383ee64778"


def walk_urls(obj, out, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk_urls(v, out, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk_urls(v, out, f"{prefix}[{i}]")
    elif isinstance(obj, str) and obj.startswith("http"):
        out.append((prefix, obj))


async def main():
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.post(f"{BASE}/file/v2/play", params={"pr": "ucpro", "fr": "pc"},
                         json={"fid": BIG_FID}, headers=api_headers())
        b = r.json()
        print(f"code={b.get('code')} msg={b.get('message')}")
        print(json.dumps(b.get("data"), ensure_ascii=False, indent=1)[:3000])

        urls = []
        walk_urls(b.get("data") or {}, urls)
        print("\n--- 提取到的 URL ---")
        for k, u in urls:
            h = urlparse(u).netloc
            print(f"[{k}] {h}\n    {u[:170]}")

        # 挑非 m3u8 的 URL 测速（优先原始画质）
        cands = [(k, u) for k, u in urls if not u.split("?")[0].endswith(".m3u8")] or urls
        for k, u in cands[:3]:
            s1 = await speed(u, with_cookie=True)
            s2 = await speed(u, with_cookie=False)
            print(f"\n[{k}] {urlparse(u).netloc}")
            print(f"  带Cookie: {s1}\n  无Cookie: {s2}")


if __name__ == "__main__":
    asyncio.run(main())
