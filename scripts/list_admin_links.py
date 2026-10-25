#!/usr/bin/env python3
"""快速列出 admin@goddamn.cool 用户的网盘文件直链。

用法:
  .venv/bin/python scripts/list_admin_links.py                 # 全库文件（空词 search）
  .venv/bin/python scripts/list_admin_links.py Breaking.Bad    # 关键词搜索
  .venv/bin/python scripts/list_admin_links.py --dir <FID>    # 展开某个目录
  .venv/bin/python scripts/list_admin_links.py --no-url        # 只列文件名不取直链
  .venv/bin/python scripts/list_admin_links.py --all          # 翻页拿全库（慢）

默认只取前 50 条，防慢。"""
import argparse
import asyncio
import os
import sys

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

from app import db, config
from app.providers.base import CloudFile
from app.providers.quark import QuarkDriveProvider

DB_PATH = config.DB_PATH
TARGET_EMAIL = "admin@goddamn.cool"


async def main():
    db.init(DB_PATH, config.SECRET_KEY)
    ap = argparse.ArgumentParser()
    ap.add_argument("keyword", nargs="?", default=None,
                    help="搜索关键词（不传或空字符串=全库）")
    ap.add_argument("--dir", default=None, metavar="FID",
                    help="展开指定目录 fid（夸克 sort 接口）")
    ap.add_argument("--limit", type=int, default=50,
                    help="单次最多文件数（默认 50）")
    ap.add_argument("--all", action="store_true",
                    help="翻页拿全库文件（慢，可能上百次 API 调用）")
    ap.add_argument("--no-url", action="store_true",
                    help="只列文件名，不取直链（省时间）")
    args = ap.parse_args()

    # 1) 找用户 + cookie
    user = db.get_user_by_email(DB_PATH, TARGET_EMAIL)
    if not user:
        print(f"❌ 找不到用户 {TARGET_EMAIL}")
        return
    cookie = db.load_user_cookie(DB_PATH, user["id"]) or ""
    if not cookie:
        print(f"❌ 用户 {TARGET_EMAIL} 未绑定 Cookie")
        return
    print(f"👤 {user['email']}  id={user['id']}  cookie_len={len(cookie)}")

    # 2) 建 provider
    p = QuarkDriveProvider(cookie, DB_PATH, user_id=user["id"])
    try:
        await p.verify()
        print("✅ Cookie 有效")
    except Exception as e:
        print(f"❌ Cookie 失效: {e}")
        return

    # 3) 收集文件（用 dict 按 fid 去重，空词 search 夸克会重复返回）
    files_by_fid: dict[str, CloudFile] = {}
    count = 0

    def add(f: CloudFile):
        if f.fid in files_by_fid:
            return
        files_by_fid[f.fid] = f

    if args.dir:
        print(f"\n📂 展开目录 {args.dir} (limit={args.limit})...")
        page = 1
        while True:
            data = await p._request("GET", "/file/sort", params={
                "pdir_fid": args.dir, "_page": str(page),
                "_size": "50", "_fetch_total": "0",
            })
            chunk = data.get("list", [])
            if not chunk:
                break
            for f in chunk:
                if f.get("dir"):
                    continue
                add(CloudFile(
                    fid=f["fid"], name=f["file_name"],
                    size=int(f.get("size") or 0), is_dir=False,
                    parent_path=f.get("parent_path", ""),
                ))
                count += 1
                if not args.all and count >= args.limit:
                    break
            print(f"  page {page}: raw={len(chunk)}, unique={len(files_by_fid)}")
            if not args.all and count >= args.limit:
                break
            if len(chunk) < 50:
                break
            page += 1
    else:
        kw = args.keyword or ""
        label = f"🔍 搜索 “{kw}”" if kw else "📂 全库文件（空词 search）"
        print(f"\n{label} (limit={args.limit})...")
        if kw:
            for f in await p.search_files(kw, limit=args.limit):
                add(f)
        else:
            page = 1
            while True:
                size = min(100, max(1, args.limit - len(files_by_fid))) if not args.all else 100
                data = await p._request("GET", "/file/search", params={
                    "query": "", "_page": str(page), "_size": str(size),
                })
                chunk = data.get("list", [])
                if not chunk:
                    break
                before = len(files_by_fid)
                for f in chunk:
                    if f.get("dir"):
                        continue
                    add(CloudFile(
                        fid=f["fid"], name=f["file_name"],
                        size=int(f.get("size") or 0), is_dir=False,
                        parent_path=f.get("parent_path", ""),
                    ))
                gained = len(files_by_fid) - before
                print(f"  page {page}: raw={len(chunk)}, +{gained} new, unique={len(files_by_fid)}")
                # 空词 search 夸克会反复返回相同文件，不再有新文件就终止
                if gained == 0:
                    break
                if not args.all and len(files_by_fid) >= args.limit:
                    break
                if len(chunk) < size:
                    break
                page += 1
                await asyncio.sleep(0.3)

    files = list(files_by_fid.values())
    if not args.all and len(files) > args.limit:
        files = files[:args.limit]

    # 4) 打印 + 取直链
    print(f"\n{'='*60}")
    print(f"📋 共 {len(files)} 个文件")
    print(f"{'='*60}")
    total = 0
    ok = 0
    fail = 0
    for i, f in enumerate(files, 1):
        size_gb = f.size / 1024 / 1024 / 1024
        total += f.size
        extra = f"  [{f.parent_path}]" if f.parent_path else ""
        print(f"\n{i:3d}. {f.name}{extra}  ({size_gb:.2f} GB)" if f.size > 1024**3
              else f"\n{i:3d}. {f.name}{extra}  ({f.size/1024/1024:.1f} MB)")
        if not args.no_url:
            try:
                res = await p.get_play_resource(f)
                print(f"     🔗 {res.url}")
                ok += 1
            except Exception as e:
                print(f"     ⚠️  取链失败: {e}")
                fail += 1
        else:
            print(f"     fid={f.fid}")
    print(f"\n{'='*60}")
    print(f"💾 总大小: {total/1024/1024/1024:.2f} GB")
    if not args.no_url:
        print(f"🔗 直链成功: {ok}  失败: {fail}")


if __name__ == "__main__":
    asyncio.run(main())
