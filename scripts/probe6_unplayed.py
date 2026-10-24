"""第六轮：验证从未播放过的小文件在 /file/v2/play 下的条目状态。"""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.play_url_probe import BASE, api_headers  # noqa: E402

FILES = [
    ("小文件(未播放)", "6e467a6dcead489e9b33cd8d670698e7"),
    ("大文件(已播放)", "e0bb2fd09dd044f8ac0dac383ee64778"),
]


async def main():
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        for label, fid in FILES:
            r = await c.post(f"{BASE}/file/v2/play", params={"pr": "ucpro", "fr": "pc"},
                             json={"fid": fid}, headers=api_headers())
            b = r.json()
            d = b.get("data") or {}
            print(f"\n== {label} fid={fid[:8]} code={b.get('code')} msg={b.get('message')}")
            print(f"   size={d.get('size')} name={d.get('file_name')}")
            for v in d.get("video_list") or []:
                vi = v.get("video_info") or {}
                print(f"   - resolution={v.get('resolution'):6} accessable={v.get('accessable')} "
                      f"trans_status={v.get('trans_status')} right={v.get('right')} "
                      f"member_right={v.get('member_right')} "
                      f"supports_format={v.get('supports_format')} "
                      f"w={vi.get('width')} size={vi.get('size')} url={'有' if vi.get('url') else '无'}")


if __name__ == "__main__":
    asyncio.run(main())
