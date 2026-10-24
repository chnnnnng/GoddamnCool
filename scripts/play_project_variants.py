"""第二轮探测：play/project 的 resolutions 参数细调 + 账号/文件大小交叉验证。

上轮结论：
- play/project 不带 resolutions -> 14018 plf_invalid；带 resolutions -> 15000 inner error
  （方向对了，参数细节不对，或与文件大小/账号有关）
- file/download 对 871MB 大文件报 23018 download file size limit

本轮：
1. 查会员状态（member 接口）
2. 列根目录文件，找一个小视频文件做对照
3. file/download 小文件 -> 验证接口与 Cookie 是否正常（区分大小限制）
4. play/project 多种 resolutions 变体打小文件 -> 找出 code=0 的组合
5. 找到可用组合后立刻打大文件复测
"""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.play_url_probe import BASE, api_headers, walk_urls  # noqa: E402

BIG_FID = "e0bb2fd09dd044f8ac0dac383ee64778"
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".flv", ".mov", ".m4v", ".webm"}


def host(u: str) -> str:
    from urllib.parse import urlparse
    return urlparse(u).netloc


async def main():
    findings = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        # 1. 会员状态
        r = await c.get(f"{BASE}/member", params={"pr": "ucpro", "fr": "pc"},
                        headers=api_headers())
        try:
            m = r.json()
            d = m.get("data") or {}
            print(f"[member] code={m.get('code')} member_type={d.get('member_type')} "
                  f"is_vip={d.get('is_vip')} vip_type={d.get('vip_type')} "
                  f"svip_expired_in={d.get('svip_expired_in')}")
        except Exception:
            print(f"[member] HTTP {r.status_code} 非 JSON: {r.text[:200]}")

        # 2. 搜索测试文件（根目录无视频，用搜索接口）
        r = await c.get(f"{BASE}/file/search",
                        params={"pr": "ucpro", "fr": "pc", "query": "余罪",
                                "_page": 1, "_size": 50},
                        headers=api_headers())
        b = r.json()
        files = (b.get("data", {}).get("list")) or []
        print(f"[search 余罪] code={b.get('code')} 命中 {len(files)} 条")
        vids = [f for f in files if not f.get("dir") and
                os.path.splitext(f.get("file_name", ""))[1].lower() in VIDEO_EXTS]
        vids.sort(key=lambda f: f.get("size") or 0)
        print("\n[根目录视频文件]")
        for f in vids[:8]:
            print(f"  {f['size']:>12} {f['fid']}  {f['file_name']}")
        small = vids[0] if vids else None
        print(f"选定的对照小文件: {small['file_name'] if small else '无'}")

        # 3. file/download 小文件对照
        if small:
            r = await c.post(f"{BASE}/file/download", params={"pr": "ucpro", "fr": "pc"},
                             json={"fids": [small["fid"]]}, headers=api_headers())
            b = r.json()
            print(f"\n[download 小文件] code={b.get('code')} msg={b.get('message')}")
            urls = []
            walk_urls(b.get("data") or {}, urls)
            for k, u in urls[:2]:
                print(f"  小文件直链域名: {host(u)}")

        # 4. play/project 变体（打小文件）
        bodies = [
            {"resolutions": ["normal", "low", "high", "super", "2k", "4k"]},
            {"resolutions": ["normal"]},
            {"resolutions": ["low"]},
            {"resolutions": ["high"]},
            {"resolutions": ["super"]},
            {"resolutions": ["2k"]},
            {"resolutions": ["low", "high", "super", "2k", "4k"]},
            {"resolutions": ["low", "high", "super", "2k", "4k"],
             "supports": ["fmp4"]},
        ]
        ok_variant = None
        for i, extra in enumerate(bodies):
            if not small:
                break
            payload = dict(extra, fids=[small["fid"]])
            r = await c.post(f"{BASE}/file/v2/play/project",
                             params={"pr": "ucpro", "fr": "pc"}, json=payload,
                             headers=api_headers())
            b = r.json()
            code, msg = b.get("code"), b.get("message")
            print(f"\n[play 小文件 #{i}] body={json.dumps(extra)} -> code={code} msg={msg}")
            if code == 0:
                ok_variant = extra
                urls = []
                walk_urls(b.get("data") or {}, urls)
                print(f"  !! 成功! data keys={list((b.get('data') or {}).keys())}")
                for k, u in urls:
                    print(f"  [{k}] {host(u)} {u[:110]}")
                findings.append({"variant": extra, "data": b.get("data")})
                break

        # 5. 成功组合打大文件
        if ok_variant and small:
            payload = dict(ok_variant, fids=[BIG_FID])
            r = await c.post(f"{BASE}/file/v2/play/project",
                             params={"pr": "ucpro", "fr": "pc"}, json=payload,
                             headers=api_headers())
            b = r.json()
            print(f"\n[play 大文件] code={b.get('code')} msg={b.get('message')}")
            urls = []
            walk_urls(b.get("data") or {}, urls)
            for k, u in urls:
                print(f"  [{k}] {host(u)} {u[:110]}")
            if b.get("code") == 0:
                findings.append({"variant_big": ok_variant, "data": b.get("data")})

    with open("/tmp/play_probe2.json", "w") as f:
        json.dump(findings, f, ensure_ascii=False, indent=2)
    print("\n已写入 /tmp/play_probe2.json")


if __name__ == "__main__":
    asyncio.run(main())
