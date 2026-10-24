"""第七轮探测：阶段 4 转存接口族（token -> detail -> save -> task 轮询）。

要回答的问题：
1. share/sharepage/token 用 pwd_id（+提取码）能否换 stoken？错误码语义？
2. share/sharepage/detail 列目录的字段结构（fid / share_fid_token / dir / 分页）？
3. share/sharepage/save 提交转存的请求体形态与响应（task_id）？
4. /task 轮询的 status 语义、save_as_top_fids 是否直接给出新文件 fid？
5. 重复转存同一文件的行为（自动改名还是报错码）？
6. 转存到根目录后 /file/search 能否立即搜到？
7. 清理：/file/delete 删除测试文件并轮询任务。
"""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import re  # noqa: E402

from scripts.play_url_probe import BASE, api_headers  # noqa: E402

PANSOU = "http://127.0.0.1:8888"  # 自部署 PanSou（阶段 3）
KEYWORD = "余罪"
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".flv", ".webm", ".mov", ".m4v"}


def short(v, n=160):
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s[:n]


async def find_live_share(c: httpx.AsyncClient) -> tuple[str, str] | None:
    """从 PanSou 搜余罪，逐个试夸克链接的 token 接口直到找到有效分享。"""
    r = await c.post(f"{PANSOU}/api/search", json={"kw": KEYWORD}, timeout=60)
    urls = re.findall(r"https?://pan\.quark\.cn/s/[0-9a-f]+", r.text)
    seen = []
    for u in urls:
        if u not in seen:
            seen.append(u)
    print(f"[pansou] {len(seen)} 个唯一夸克分享链接，逐个验证…")
    for i, u in enumerate(seen[:15]):
        pwd_id = u.rsplit("/", 1)[-1]
        r = await c.post(f"{BASE}/share/sharepage/token", params={"pr": "ucpro", "fr": "pc"},
                         json={"pwd_id": pwd_id, "passcode": ""}, headers=api_headers())
        b = r.json()
        print(f"  [{i}] {pwd_id} -> code={b.get('code')} msg={b.get('message')}")
        if b.get("code") == 0:
            return pwd_id, b["data"]["stoken"]
    return None


async def main():
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        h = api_headers()

        # ---- 1. token：pwd_id + 提取码 → stoken（自动从 PanSou 找有效分享）----
        got = await find_live_share(c)
        if not got:
            print("未找到有效分享链接")
            return
        PWD_ID, stoken = got
        print(f"使用分享: {PWD_ID}")
        print("  stoken:", stoken[:24], "... (len", len(stoken), ")")

        # ---- 2. detail：列分享目录（递归两层找视频文件）----
        videos = []

        async def list_dir(fid: str, depth: int = 0):
            r = await c.get(f"{BASE}/share/sharepage/detail", params={
                "pr": "ucpro", "fr": "pc", "pwd_id": PWD_ID, "stoken": stoken,
                "pdir_fid": fid, "_page": "1", "_size": "50",
                "_fetch_banner": "1", "_fetch_share": "1", "_fetch_total": "1",
                "_sort": "file_type:asc,file_name:asc"}, headers=h)
            b = r.json()
            print(f"[detail d{depth} fid={fid[:8]}] code={b.get('code')} "
                  f"total={short((b.get('data') or {}).get('_total'))}")
            if b.get("code") != 0:
                return
            for e in (b["data"].get("list") or []):
                if depth == 0 and e is (b["data"].get("list") or [None])[0]:
                    print("  entry keys:", sorted(e.keys()))
                if e.get("dir"):
                    if depth < 2:
                        await list_dir(e["fid"], depth + 1)
                elif os.path.splitext(e.get("file_name", ""))[1].lower() in VIDEO_EXTS:
                    videos.append(e)

        await list_dir("0")
        print(f"共发现视频文件 {len(videos)} 个")
        if not videos:
            return
        target = min(videos, key=lambda e: int(e.get("size") or 0))  # 挑最小的转存
        print(f"转存目标: {target['file_name']} size={target['size']}")
        print("  fid:", target["fid"])
        print("  share_fid_token:", short(target.get("share_fid_token"), 60))

        # ---- 3. save：提交转存到根目录（完整 dump 定位 fid 字段）----
        r = await c.post(f"{BASE}/share/sharepage/save", params={"pr": "ucpro", "fr": "pc"},
                         json={"fid_list": [target["fid"]],
                               "fid_token_list": [target.get("share_fid_token")],
                               "to_pdir_fid": "0", "pwd_id": PWD_ID, "stoken": stoken,
                               "pdir_fid": "0", "scene": "link"}, headers=h)
        b = r.json()
        print(f"[save] HTTP {r.status_code} code={b.get('code')} msg={b.get('message')}")
        print("  data:", json.dumps(b.get("data"), ensure_ascii=False))
        if b.get("code") != 0:
            return
        task_id = (b.get("data") or {}).get("task_id")
        if not task_id:
            return

        # ---- 4. task 轮询（完整 dump）----
        for i in range(15):
            await asyncio.sleep(1)
            r = await c.get(f"{BASE}/task", params={
                "pr": "ucpro", "fr": "pc", "task_id": task_id, "retry_index": str(i)}, headers=h)
            b = r.json()
            print(f"[task #{i}] code={b.get('code')} "
                  f"data={json.dumps(b.get('data'), ensure_ascii=False)}")
            d = b.get("data") or {}
            if b.get("code") == 0 and d.get("status") in (2, 3):  # 2=成功 3=失败（待确认）
                break

        # ---- 5. 转存结果文件能否搜到 ----
        r = await c.get(f"{BASE}/file/search", params={
            "pr": "ucpro", "fr": "pc", "query": os.path.splitext(target["file_name"])[0],
            "_page": "1", "_size": "10"}, headers=h)
        b = r.json()
        found = [(f["fid"], f["file_name"]) for f in (b.get("data") or {}).get("list", [])]
        print(f"[search] code={b.get('code')} 找到 {len(found)} 个")
        for fid, name in found[:3]:
            print("   ", fid, name)

        # ---- 6. 重复转存（幂等行为）----
        r = await c.post(f"{BASE}/share/sharepage/save", params={"pr": "ucpro", "fr": "pc"},
                         json={"fid_list": [target["fid"]],
                               "fid_token_list": [target.get("share_fid_token")],
                               "to_pdir_fid": "0", "pwd_id": PWD_ID, "stoken": stoken,
                               "pdir_fid": "0", "scene": "link"}, headers=h)
        b = r.json()
        print(f"[save again] code={b.get('code')} msg={b.get('message')} "
              f"data={short(b.get('data'), 200)}")
        task_id2 = (b.get("data") or {}).get("task_id")
        if task_id2:
            for i in range(10):
                await asyncio.sleep(1)
                r = await c.get(f"{BASE}/task", params={
                    "pr": "ucpro", "fr": "pc", "task_id": task_id2, "retry_index": str(i)},
                    headers=h)
                b = r.json()
                d = b.get("data") or {}
                print(f"[task2 #{i}] status={d.get('status')} "
                      f"save_as_top_fids={short(d.get('save_as_top_fids'))}")
                if b.get("code") == 0 and d.get("status") in (2, 3):
                    break

        # ---- 7. 清理：删除转存的文件（14001 提示参数名是 filelist）----
        r = await c.get(f"{BASE}/file/search", params={
            "pr": "ucpro", "fr": "pc", "query": target["file_name"],
            "_page": "1", "_size": "50"}, headers=h)
        entries = (r.json().get("data") or {}).get("list", [])
        # 仅删除与目标文件名完全一致或自动改名副本（如 "06 (1).mkv"），防止误删
        stem, ext = os.path.splitext(target["file_name"])
        names = {stem + ext, f"{stem} (1){ext}", f"{stem}(1){ext}"}
        fids = [f["fid"] for f in entries if f["file_name"] in names]
        print(f"[cleanup] 待删除: {[f['file_name'] for f in entries if f['file_name'] in names]}")
        if not fids:
            print("[cleanup] 未找到待删除文件")
            return
        r = await c.post(f"{BASE}/file/delete", params={"pr": "ucpro", "fr": "pc"},
                         json={"filelist": fids, "exclude_fids": []}, headers=h)
        b = r.json()
        print(f"[delete] code={b.get('code')} msg={b.get('message')} data={short(b.get('data'), 200)}")
        task_id3 = (b.get("data") or {}).get("task_id")
        if task_id3:
            for i in range(10):
                await asyncio.sleep(1)
                r = await c.get(f"{BASE}/task", params={
                    "pr": "ucpro", "fr": "pc", "task_id": task_id3, "retry_index": str(i)},
                    headers=h)
                d = (r.json().get("data") or {})
                print(f"[task3 #{i}] status={d.get('status')}")
                if r.json().get("code") == 0 and d.get("status") in (2, 3):
                    break


if __name__ == "__main__":
    asyncio.run(main())
