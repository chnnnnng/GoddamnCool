"""第八轮探测：目录创建/删除接口（决定阶段 4 转存目标目录方案）。

1. POST /file 能否在根目录创建 "Stremio" 文件夹（fid）？
2. 重复创建同名文件夹的行为（自动改名 or 报错）？
3. 用 filelist 删除文件夹（清理现场）。
"""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.play_url_probe import BASE, api_headers  # noqa: E402

DIR_NAME = "Stremio转存测试-可删"


async def main():
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        h = api_headers()

        r = await c.post(f"{BASE}/file", params={"pr": "ucpro", "fr": "pc"},
                         json={"pdir_fid": "0", "file_name": DIR_NAME,
                               "dir_path": "", "dir_init_lock": False}, headers=h)
        b = r.json()
        print(f"[mkdir] code={b.get('code')} msg={b.get('message')}")
        print("  data:", json.dumps(b.get("data"), ensure_ascii=False)[:300])
        if b.get("code") != 0:
            return
        fid = b["data"]["fid"]

        r = await c.post(f"{BASE}/file", params={"pr": "ucpro", "fr": "pc"},
                         json={"pdir_fid": "0", "file_name": DIR_NAME,
                               "dir_path": "", "dir_init_lock": False}, headers=h)
        b = r.json()
        print(f"[mkdir again] code={b.get('code')} msg={b.get('message')} "
              f"name={((b.get('data') or {}).get('file_name'))}")

        # 在该目录里建一个子目录测试嵌套（验证 pdir_fid 语义），随后一并清理
        r = await c.post(f"{BASE}/file", params={"pr": "ucpro", "fr": "pc"},
                         json={"pdir_fid": fid, "file_name": "子目录",
                               "dir_path": "", "dir_init_lock": False}, headers=h)
        b = r.json()
        print(f"[mkdir sub] code={b.get('code')} msg={b.get('message')}")

        r = await c.get(f"{BASE}/file/search", params={
            "pr": "ucpro", "fr": "pc", "query": DIR_NAME,
            "_page": "1", "_size": "50"}, headers=h)
        found = [(f["fid"], f["file_name"]) for f in (r.json().get("data") or {}).get("list", [])
                 if f["file_name"].startswith(DIR_NAME)]
        print(f"[search] {found}")

        r = await c.post(f"{BASE}/file/delete", params={"pr": "ucpro", "fr": "pc"},
                         json={"filelist": [f[0] for f in found], "exclude_fids": []}, headers=h)
        b = r.json()
        print(f"[delete] code={b.get('code')} msg={b.get('message')}")


if __name__ == "__main__":
    asyncio.run(main())
