"""端到端测试：搜索"绝命毒师"，打印 AI 完整交互链路。

运行：cd /root/workspace && .venv/bin/python scripts/test_ai_chain.py
需要生产 DB（data/quark_stremio.db）和已配置 LLM。"""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/root/workspace")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

# 指向生产库
os.environ["DB_PATH"] = "/root/workspace/data/quark_stremio.db"

from app import config, db, llm, settings  # noqa: E402
from app.name_parser import parser as name_parser  # noqa: E402

settings.load(config.DB_PATH)  # 从生产 DB 读 LLM 配置


KW = "绝命毒师"


async def main():
    print(f"=== 1. llm.configured(): {llm.configured()} ===")
    if not llm.configured():
        print("LLM 未配置，退出")
        return

    from app.candidate_service import CandidateService
    svc = CandidateService()
    cookie = ""  # 匿名搜索即可

    print(f"\n=== 2. CandidateService.search('{KW}') ===")
    meta, cands, errors, final_kw = await svc.search(
        "movie", "", keyword=KW, disks=None, cookie=cookie)
    print(f"   meta: {meta.title if meta else None}  keyword: {final_kw}")
    print(f"   candidates: {len(cands)}")
    for c in cands:
        d = getattr(c, "detail", None) or {}
        print(f"   - [{c.disk}] {c.title[:60]}  vc={d.get('video_count')}  files={len(d.get('files') or [])}")

    # 取第一个有 files 的候选做 AI enrich
    target = None
    for c in cands:
        d = getattr(c, "detail", None) or {}
        if d.get("files"):
            target = c
            break
    if not target:
        print("\n没有候选带 files，无法测试 AI enrich")
        return

    d = target.detail
    VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".ts",
                 ".m2ts", ".mpg", ".mpeg", ".m4v", ".iso"}
    vf = [f for f in d["files"]
          if os.path.splitext((f.get("path") or f.get("name") or ""))[1].lower() in VIDEO_EXT]
    vf = vf[:40]
    paths = [f.get("path") or f.get("name") or "" for f in vf]

    print(f"\n=== 3. 原始文件路径（{len(paths)} 个） ===")
    for p in paths[:5]:
        print(f"   {p}")
    if len(paths) > 5:
        print(f"   ...（共 {len(paths)}）")

    compact, mapping = llm.compact_file_paths(paths)
    print(f"\n=== 4. compact_file_paths 合并后（{len(compact)} 个模板） ===")
    for i, (cp, m) in enumerate(zip(compact, mapping)):
        names = [os.path.basename(paths[j]) for j in m]
        print(f"   [{i}] {cp}  ← {names}")

    send_cands = [{
        "title": target.title,
        "source": target.source,
        "published": target.published,
        "alive_state": target.alive_state,
        "video_count": d.get("video_count"),
        "total_size_h": d.get("total_size_h"),
        "files": [{"path": p} for p in compact],
    }]
    print(f"\n=== 5. 喂给 llm.review_candidates 的 payload ===")
    print(json.dumps({"keyword": KW, "title": KW, "season": None,
                      "candidates": send_cands}, ensure_ascii=False, indent=2)[:2000])

    print(f"\n=== 6. 调真实 AI（ REVIEW_SKILL_VERSION={llm.REVIEW_SKILL_VERSION}） ===")
    import time
    t0 = time.monotonic()
    try:
        items = await llm.review_candidates(
            send_cands, keyword=KW, title=KW, season=None)
        dt = time.monotonic() - t0
        print(f"   ✅ 耗时 {dt:.1f}s")
    except llm.LLMError as e:
        dt = time.monotonic() - t0
        print(f"   ❌ 耗时 {dt:.1f}s  LLMError: {e}")
        return

    print(f"\n=== 7. AI 返回的原始 JSON（items） ===")
    print(json.dumps(items, ensure_ascii=False, indent=2)[:3000])

    it = items[0]
    print(f"\n=== 8. 展开回填：每个原始文件的元数据 + 拼接规范名 ===")
    files_by = {pf["ci"]: pf for pf in (it.get("files") or [])}
    for compact_i, orig_indices in enumerate(mapping):
        pf = files_by.get(compact_i)
        if not pf:
            print(f"   [{compact_i}] 无 AI 输出 → 跳过")
            continue
        for orig_i in orig_indices:
            if orig_i >= len(vf):
                continue
            f = vf[orig_i]
            orig_name = os.path.basename(f.get("path") or f.get("name") or "")
            info = name_parser.parse(orig_name)
            ai_title = pf.get("title") or info.title or info.title_en
            ai_season = pf.get("season") if pf.get("season") is not None else info.season
            ai_episode = pf.get("episode") if pf.get("episode") is not None else info.episode
            ai_year = pf.get("year") if pf.get("year") is not None else info.year
            canonical = name_parser.canonical(
                info, title=ai_title, season=ai_season,
                episode=ai_episode, year=ai_year)
            structured = {
                "title": ai_title, "year": ai_year,
                "season": ai_season, "episode": ai_episode,
                "resolution": pf.get("resolution") or info.resolution,
                "source": pf.get("source") or info.source,
                "codec": pf.get("codec") or info.codec,
                "audio": pf.get("audio") or (info.audio[0] if info.audio else None),
                "group": pf.get("group") or (info.group[0] if info.group else None),
            }
            print(f"\n   原始: {orig_name}")
            print(f"   AI 返回 ci={compact_i}: title={pf.get('title')} season={pf.get('season')} episode={pf.get('episode')}")
            print(f"   合并元数据: {json.dumps(structured, ensure_ascii=False)}")
            print(f"   规则解析 canonical: {name_parser.canonical(info)}")
            print(f"   AI 元数据 canonical: {canonical}")


asyncio.run(main())
