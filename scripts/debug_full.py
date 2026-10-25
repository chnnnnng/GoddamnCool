"""端到端调试：完整打印 PanSou → compact → AI → 回填 4 阶段内容。

运行：cd /root/workspace && .venv/bin/python scripts/debug_full.py
"""
import asyncio
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("DB_PATH", "/root/workspace/data/quark_stremio.db")

from app import config, llm, settings  # noqa
from app.name_parser import parser as name_parser  # noqa
from app.candidate_service import CandidateService  # noqa

KW = "绝命毒师"


def _clear_cache() -> None:
    con = sqlite3.connect(config.DB_PATH)
    n = con.execute("DELETE FROM kv_cache WHERE key LIKE 'llm:%'").rowcount
    con.commit()
    if n:
        print(f"[缓存] 清掉 {n} 条 llm 缓存")


async def main():
    settings.load(config.DB_PATH)
    _clear_cache()

    # ---- 阶段 1：PanSou 返回（原始 inspect_share 结果） ----
    print(f"\n{'='*20} 阶段 1：PanSou 返回（前 5 个候选 + 第 1 个候选的全部 files）{'='*20}")
    svc = CandidateService()
    t0 = time.monotonic()
    meta, cands, errors, final_kw = await svc.search("movie", "", keyword=KW, disks=None, cookie="")
    print(f"PanSou 搜索耗时 {time.monotonic()-t0:.1f}s  候选 {len(cands)} 个  error={errors}")
    for ci, c in enumerate(cands[:5]):
        d = getattr(c, "detail", None) or {}
        files = d.get("files") or []
        print(f"  [{ci}] [{c.disk}] {c.title[:50]}  vc={d.get('video_count')}  tsize={d.get('total_size_h')}")
    # 取第一个有 files 的候选展开
    target = None
    for c in cands:
        d = getattr(c, "detail", None) or {}
        if d.get("files"):
            target = c
            break
    t_d = target.detail
    VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".ts", ".m2ts",
                 ".mpg", ".mpeg", ".m4v", ".iso"}
    vf = [f for f in t_d["files"]
          if os.path.splitext((f.get("path") or f.get("name") or ""))[1].lower() in VIDEO_EXT]
    paths = [f.get("path") or f.get("name") or "" for f in vf]
    print(f"\n候选 [0] 总 files={len(t_d['files'])}  视频 files={len(vf)}")
    print(f"原始路径（前 5）：")
    for p in paths[:5]:
        print(f"  {p}")

    # ---- 阶段 2：发送给 AI 的 payload（含 compact 结果）----
    print(f"\n{'='*20} 阶段 2：compact_file_paths + 发给 AI 的 payload {'='*20}")
    compact, mapping = llm.compact_file_paths(paths)
    print(f"原始 {len(paths)} 条 → 压缩成 {len(compact)} 条")
    for i, (cp, m) in enumerate(zip(compact, mapping)):
        print(f"  [{i}] {len(m)} 条合并 → {cp[:150]}{'...' if len(cp)>150 else ''}")

    send_cands = [{
        "index": 0,  # 调 review_candidates 只传一个候选
        "title": target.title,
        "source": target.source,
        "published": target.published,
        "alive_state": target.alive_state,
        "video_count": t_d.get("video_count"),
        "total_size_h": t_d.get("total_size_h"),
        "files": [{"path": p} for p in compact],
    }]
    # 也把其他候选一起塞进去，模拟实际 AISearchChain 行为
    all_cands = []
    for ci2, c in enumerate(cands):
        d = getattr(c, "detail", None) or {}
        vf2 = [f for f in (d.get("files") or [])
               if os.path.splitext((f.get("path") or f.get("name") or ""))[1].lower() in VIDEO_EXT]
        paths2 = [f.get("path") or f.get("name") or "" for f in vf2]
        cp2, _ = llm.compact_file_paths(paths2)
        all_cands.append({
            "title": c.title, "source": c.source, "published": c.published,
            "alive_state": c.alive_state,
            "video_count": d.get("video_count"),
            "total_size_h": d.get("total_size_h"),
            "files": [{"path": p} for p in cp2],
        })
    payload_for_ai = {"keyword": KW, "title": KW, "season": None, "candidates": all_cands}
    print(f"\n=== 完整 AI payload（候选 {len(all_cands)} 个，总 files 模板 {sum(len(c['files']) for c in all_cands)} 条）===")
    print(json.dumps(payload_for_ai, ensure_ascii=False, indent=2)[:3000])
    print(f"...（截断，完整大小 {len(json.dumps(payload_for_ai, ensure_ascii=False))} 字节）")

    # ---- 阶段 3：AI 返回 ----
    print(f"\n{'='*20} 阶段 3：调用 AI + AI 返回 {'='*20}")
    t0 = time.monotonic()
    items = await llm.review_candidates(all_cands, keyword=KW, title=KW, season=None)
    dt = time.monotonic() - t0
    print(f"AI 耗时 {dt:.1f}s  返回 {len(items)} 条")
    for ci2, it in enumerate(items):
        print(f"\n--- 候选 [{ci2}] keep={it.get('keep')} score={it.get('score')} dup_of={it.get('dup_of')} ---")
        print(f"  reason: {it.get('reason')}")
        print(f"  title(顶层): {it.get('title')}  year: {it.get('year')}  season: {it.get('season')}")
        print(f"  files 条目数: {len(it.get('files') or [])}")
        for pf in (it.get("files") or [])[:3]:
            print(f"    ci={pf.get('ci')} title={pf.get('title')} S={pf.get('season')} E={pf.get('episode')} year={pf.get('year')} res={pf.get('resolution')} src={pf.get('source')} grp={pf.get('group')}")
        if len(it.get("files") or []) > 3:
            print(f"    ... 还有 {len(it['files'])-3} 条")

    # ---- 阶段 4：后处理回填 ----
    print(f"\n{'='*20} 阶段 4：后处理回填（前 keep=true 候选的展开）{'='*20}")
    # 重建 AISearchChain 的 enrich 逻辑
    # 先拿候选 [0] 的 compact/mapping/vf
    files_by = {pf["ci"]: pf for pf in (items[0].get("files") or [])}
    expanded = []
    for compact_i, orig_indices in enumerate(mapping):
        pf = files_by.get(compact_i)
        if not pf:
            expanded.append({"compact_i": compact_i, "pf": None, "count": len(orig_indices)})
            continue
        for orig_i in orig_indices[:3]:  # 每模板只看前 3 条样本
            if orig_i >= len(vf):
                continue
            f = vf[orig_i]
            orig_name = os.path.basename(f.get("path") or f.get("name") or "")
            info = name_parser.parse(orig_name)
            ai_title = pf.get("title") or info.title or info.title_en
            ai_season = pf.get("season") if pf.get("season") is not None else info.season
            ai_episode = pf.get("episode") if pf.get("episode") is not None else info.episode
            ai_year = pf.get("year") if pf.get("year") is not None else info.year
            canonical_ai = name_parser.canonical(info, title=ai_title, season=ai_season,
                                                  episode=ai_episode, year=ai_year)
            canonical_rule = name_parser.canonical(info)
            expanded.append({
                "compact_i": compact_i,
                "原始文件名": orig_name,
                "AI 返回 title": pf.get("title"),
                "AI 返回 S/E": f"S={pf.get('season')} E={pf.get('episode')}",
                "规则 canonical": canonical_rule,
                "AI 元数据 canonical": canonical_ai,
            })
        if len(orig_indices) > 3:
            expanded.append({"compact_i": compact_i, "_note": f"... 此模板还有 {len(orig_indices)-3} 条"})

    # 去重展示
    shown_compact = set()
    for e in expanded:
        ci2 = e["compact_i"]
        if ci2 in shown_compact and "_note" not in e:
            continue
        shown_compact.add(ci2)
        print(f"\n  模板 ci={ci2}")
        for k, v in e.items():
            if k == "compact_i":
                continue
            print(f"    {k}: {v}")


asyncio.run(main())
