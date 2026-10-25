"""端到端调试脚本：打印 PanSou → compact → AI → 后处理 四阶段内容。

用法：
  cd /root/workspace
  .venv/bin/python tests/debug_ai_chain.py                      # 默认搜索"绝命毒师"
  .venv/bin/python tests/debug_ai_chain.py "狂飙"
  .venv/bin/python tests/debug_ai_chain.py "绝命毒师" --season 1
  .venv/bin/python tests/debug_ai_chain.py "绝命毒师" --no-cache  # 清 llm 缓存

需要：生产 DB (data/quark_stremio.db) 已配置 LLM。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time

# 把项目根加进 sys.path（独立运行不依赖 pytest 上下文）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("DB_PATH", os.path.join(_ROOT, "data", "quark_stremio.db"))

from app import config, llm, settings  # noqa: E402
from app.name_parser import parser as name_parser  # noqa: E402


VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".ts",
             ".m2ts", ".mpg", ".mpeg", ".m4v", ".iso"}


def _clear_llm_cache() -> int:
    con = sqlite3.connect(config.DB_PATH)
    n = con.execute("DELETE FROM kv_cache WHERE key LIKE 'llm:%'").rowcount
    con.commit()
    return n


async def run(keyword: str, season: int | None, *, clear_cache: bool) -> None:
    settings.load(config.DB_PATH)

    if clear_cache:
        n = _clear_llm_cache()
        print(f"[缓存] 清掉 {n} 条 llm 缓存")
    print(f"[LLM 配置] configured={llm.configured()}  REVIEW_SKILL_VERSION={llm.REVIEW_SKILL_VERSION}")

    from app.search.search_service import SearchService
    svc = SearchService()

    # ============================================================
    # 阶段 1: PanSou 返回 + inspect_share 结果
    # ============================================================
    print(f"\n{'='*70}")
    print(f"阶段 1: PanSou 返回  keyword='{keyword}'  season={season}")
    print(f"{'='*70}")

    t0 = time.monotonic()
    meta, cands, errors, final_kw = await svc.search(
        "movie", "", keyword=keyword, disks=None, cookie="")
    dt = time.monotonic() - t0
    print(f"PanSou 耗时 {dt:.1f}s  候选 {len(cands)} 个  errors={errors!r}")

    # 存活分布
    alive_cnt = {}
    for c in cands:
        s = getattr(c, "alive_state", None) or "-"
        alive_cnt[s] = alive_cnt.get(s, 0) + 1
    print(f"alive_state 分布: {alive_cnt}")

    for ci, c in enumerate(cands[:6]):
        d = getattr(c, "detail", None) or {}
        files = d.get("files") or []
        seasons = d.get("seasons") or []
        print(f"  [{ci}] [{getattr(c,'alive_state','-'):10s}] seasons={len(seasons):2d} "
              f"vc={d.get('video_count')} tsize={d.get('total_size_h')} "
              f"files={len(files):3d}  {c.title[:55]}")

    # 取第一个有 files 的候选做完整展开
    target = None
    for c in cands:
        if (getattr(c, "detail", None) or {}).get("files"):
            target = c
            break
    if not target:
        print("⚠️ 没有候选带 files，后续跳过")
        return

    d = target.detail
    vf = [f for f in d["files"]
          if os.path.splitext((f.get("path") or f.get("name") or ""))[1].lower() in VIDEO_EXT]
    paths = [f.get("path") or f.get("name") or "" for f in vf]
    print(f"\n候选 [{target.title[:45]}] 完整：")
    print(f"  video files = {len(vf)}")
    for p in paths[:5]:
        print(f"    {p}")
    if len(paths) > 5:
        print(f"    ... 共 {len(paths)} 条")

    # ============================================================
    # 阶段 2: compact_file_paths + 发送给 AI 的 payload
    # ============================================================
    print(f"\n{'='*70}")
    print(f"阶段 2: compact_file_paths + 发给 AI 的 payload")
    print(f"{'='*70}")

    compact, mapping = llm.compact_file_paths(paths)
    print(f"\n候选 [0] 压缩: 原始 {len(paths)} 条 → {len(compact)} 条")
    for i, (cp, m) in enumerate(zip(compact, mapping)):
        print(f"  [{i}] {len(m):3d} 条 →")
        print(f"      {cp[:200]}{'...' if len(cp)>200 else ''}")

    # 模拟 AISearchChain 实际发送：构造完整 payload
    compact_per_cand: list[tuple[list[str], list[list[int]], list[dict]]] = []
    send_cands: list[dict] = []
    alive_indices: list[int] = []
    for ci2, c in enumerate(cands):
        d2 = getattr(c, "detail", None) or {}
        files2 = d2.get("files") or []
        vf2 = [f for f in files2
               if os.path.splitext((f.get("path") or f.get("name") or ""))[1].lower() in VIDEO_EXT]
        paths2 = [f.get("path") or f.get("name") or "" for f in vf2]
        cp2, mp2 = llm.compact_file_paths(paths2)
        compact_per_cand.append((cp2, mp2, vf2))
        if getattr(c, "alive_state", None) != "ok":
            continue
        send_cands.append({
            "title": getattr(c, "title", None),
            "source": getattr(c, "source", None),
            "published": getattr(c, "published", None),
            "alive_state": getattr(c, "alive_state", None),
            "video_count": d2.get("video_count"),
            "total_size_h": d2.get("total_size_h"),
            "files": [{"path": p} for p in cp2],
        })
        alive_indices.append(ci2)

    payload = {"keyword": keyword, "title": keyword, "season": season,
               "candidates": send_cands}
    total_files_templates = sum(len(c["files"]) for c in send_cands)
    print(f"\n=== 发给 AI 的完整 payload（已按 alive 过滤）===")
    print(f"  候选 {len(send_cands)} 个（原 {len(cands)}，非 ok 过滤掉 {len(cands)-len(send_cands)}）")
    print(f"  总 files 模板 {total_files_templates} 条")
    print(f"  payload 大小 {len(json.dumps(payload, ensure_ascii=False))} 字节")
    print(f"  前 2000 字节：")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])

    # ============================================================
    # 阶段 3: 调用 AI + AI 返回
    # ============================================================
    print(f"\n{'='*70}")
    print(f"阶段 3: 调用 AI（REVIEW_SKILL_VERSION={llm.REVIEW_SKILL_VERSION}）")
    print(f"{'='*70}")

    try:
        t0 = time.monotonic()
        items = await llm.review_candidates(
            send_cands, keyword=keyword, title=keyword, season=season)
        dt = time.monotonic() - t0
        print(f"AI 耗时 {dt:.1f}s  返回 {len(items)} 条\n")
    except llm.LLMError as e:
        print(f"❌ AI 调用失败: {e}")
        return

    for send_idx, it in enumerate(items):
        ci2 = alive_indices[send_idx]
        keep_mark = "✅keep" if it.get("keep") else "❌drop"
        print(f"--- [{ci2}] {keep_mark} score={it.get('score')} dup_of={it.get('dup_of')} ---")
        print(f"  reason: {it.get('reason')}")
        print(f"  title={it.get('title')}  year={it.get('year')}  season={it.get('season')}")
        pf_list = it.get("files") or []
        print(f"  files 条目数: {len(pf_list)}")
        for pf in pf_list[:3]:
            print(f"    ci={pf.get('ci')} title={pf.get('title')} S={pf.get('season')} "
                  f"E={pf.get('episode')} year={pf.get('year')} "
                  f"res={pf.get('resolution')} src={pf.get('source')} grp={pf.get('group')}")
        if len(pf_list) > 3:
            print(f"    ... 还有 {len(pf_list)-3} 条")

    # ============================================================
    # 阶段 4: 后处理回填（看 keep=true 候选的展开）
    # ============================================================
    print(f"\n{'='*70}")
    print(f"阶段 4: 后处理回填（keep=true 候选的展开）")
    print(f"{'='*70}")

    n_ai_total, n_rule_total = 0, 0
    for send_idx, it in enumerate(items):
        ci2 = alive_indices[send_idx]
        compact2, mapping2, vf2 = compact_per_cand[ci2]
        files_by = {pf["ci"]: pf for pf in (it.get("files") or [])}
        if it.get("keep") is False:
            print(f"\n[{ci2}] keep=false → 跳过 AI 回填（走规则 canonical）")
            continue
        print(f"\n[{ci2}] keep=true 展开样本：")
        shown = 0
        for compact_i, orig_indices in enumerate(mapping2):
            pf = files_by.get(compact_i)
            if not pf:
                continue
            for orig_i in orig_indices[:2]:
                if orig_i >= len(vf2):
                    continue
                f = vf2[orig_i]
                orig_name = os.path.basename(f.get("path") or f.get("name") or "")
                info = name_parser.parse(orig_name)
                ai_title = pf.get("title") or info.title or info.title_en
                ai_season = pf.get("season") if pf.get("season") is not None else info.season
                ai_episode = pf.get("episode") if pf.get("episode") is not None else info.episode
                ai_year = pf.get("year") if pf.get("year") is not None else info.year
                canonical_ai = name_parser.canonical(
                    info, title=ai_title, season=ai_season,
                    episode=ai_episode, year=ai_year)
                canonical_rule = name_parser.canonical(info)
                n_ai_total += 1
                print(f"\n  原始: {orig_name}")
                print(f"  AI   title={pf.get('title')} S={pf.get('season')} E={pf.get('episode')} "
                      f"res={pf.get('resolution')} grp={pf.get('group')}")
                print(f"  规则 canonical (无片名): {canonical_rule}")
                print(f"  AI   canonical (带片名): {canonical_ai}")
                shown += 1
                if shown >= 4:
                    break
            if shown >= 4:
                break

    print(f"\n{'='*70}")
    print(f"总结")
    print(f"{'='*70}")
    print(f"  AI 解析回填文件: {n_ai_total} 个（keep=true 候选）")
    print(f"  规则 canonical 兜底候选: keep=false 的全部候选")
    print(f"  payload 压缩率: 原始 {sum(len(p) for p in [paths]) if 'paths' in dir() else 0} 条路径 → 模板 {len(compact)} 条")


def main():
    parser = argparse.ArgumentParser(description="AI 链路端到端调试")
    parser.add_argument("keyword", nargs="?", default="绝命毒师", help="搜索关键词")
    parser.add_argument("--season", type=int, default=None, help="指定季数")
    parser.add_argument("--no-cache", action="store_true", help="清 llm 缓存后调真实 AI")
    args = parser.parse_args()

    asyncio.run(run(args.keyword, args.season, clear_cache=args.no_cache))


if __name__ == "__main__":
    main()
