"""第五轮：/file/v2/play 响应结构细节 + 画质/Range/Content-Type 验证。"""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.play_url_probe import BASE, COOKIE, PC_UA, api_headers  # noqa: E402

BIG_FID = "e0bb2fd09dd044f8ac0dac383ee64778"


async def main():
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.post(f"{BASE}/file/v2/play", params={"pr": "ucpro", "fr": "pc"},
                         json={"fid": BIG_FID}, headers=api_headers())
        d = r.json().get("data") or {}
        top = {k: v for k, v in d.items() if k not in
               ("video_list", "audio_list", "seek_preview_data")}
        print("== 顶层字段 ==")
        print(json.dumps(top, ensure_ascii=False, indent=1)[:1500])
        print("\n== video_list 各画质 ==")
        for v in d.get("video_list") or []:
            vi = v.get("video_info") or {}
            print(f"- resolution={v.get('resolution')} keys={list(v.keys())}")
            print(f"  video_info: duration={vi.get('duration')} width={vi.get('width')} "
                  f"height={vi.get('height')} size={vi.get('size')} "
                  f"bitrate={vi.get('bitrate')} format={vi.get('format_name') or vi.get('format')}")
            print(f"  url_host_keys={[k for k in vi.keys() if 'url' in k.lower()]}")
        print(f"\n== audio_list ==\n{json.dumps(d.get('audio_list'), ensure_ascii=False)[:400]}")

        # 对第一个 URL 做 Range/Content-Type 验证
        first = ((d.get("video_list") or [{}])[0].get("video_info") or {}).get("url")
        if first:
            hdrs = {"Cookie": COOKIE, "Referer": "https://pan.quark.cn/", "User-Agent": PC_UA}
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=hdrs) as c2:
                rr = await c2.get(first, headers={"Range": "bytes=0-2047"})
                print(f"\n== Range 请求（带Cookie）==")
                print(f"HTTP {rr.status_code}")
                for h in ("content-type", "content-length", "content-range",
                          "accept-ranges", "content-disposition"):
                    if h in rr.headers:
                        print(f"{h}: {rr.headers[h]}")
                rr2 = await c2.head(first)
                print(f"\nHEAD: HTTP {rr2.status_code}")
                for h in ("content-type", "content-length", "accept-ranges"):
                    if h in rr2.headers:
                        print(f"{h}: {rr2.headers[h]}")


if __name__ == "__main__":
    asyncio.run(main())
