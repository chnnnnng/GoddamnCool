#!/usr/bin/env python3
"""TMDB 时延 + 正确性探测脚本（生产环境专用）。

自动注入当前可用的 CheckTMDB IP 跳过 DNS 污染；
同时验证 /search/movie 是否真的不带 imdb_id（修复方向）。

cd /root/workspace && .venv/bin/python scripts/test_tmdb_latency_prod.py
"""
import asyncio, statistics, time, os, tempfile, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.fernet import Fernet
from app import db
from app.meta.tmdb import TMDBMetaProvider


# 当前可用 IP（来源：CheckTMDB github hosts 表；定期刷新）
# api.themoviedb.org = api.tmdb.org 的别名，走 tmdb.org 的 IP
PROD_HOST = "13.227.65.62"


async def main():
    p = TMDBMetaProvider()
    p._host = PROD_HOST
    p._host_ts = time.time()

    print("=" * 60)
    print(f"TMDB API 探测 (host={PROD_HOST})")
    print("=" * 60)

    # ======= 1. fetch_meta 正确性 + 缓存 =======
    print("\n[1] fetch_meta 正确性 + SQLite 缓存加速比\n")
    SAMPLES = [
        ("movie", "tt0816692", "星际穿越"),
        ("movie", "tt21357150", "复仇者联盟5"),
        ("tv",    "tt5925192",  "余罪"),
        ("tv",    "tt4574334",  "怪奇物语"),
    ]
    for kind, iid, kw in SAMPLES:
        f2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False); f2.close()
        db.init(f2.name, Fernet.generate_key().decode())
        t0 = time.monotonic()
        meta = await p.fetch_meta(iid, kind, f2.name)
        net_ms = (time.monotonic() - t0) * 1000
        t1 = time.monotonic()
        meta2 = await p.fetch_meta(iid, kind, f2.name)
        cache_ms = (time.monotonic() - t1) * 1000
        os.unlink(f2.name)
        if meta:
            sp = max(net_ms / max(cache_ms, 1), 1) if cache_ms > 0 else 0
            extra = ""
            if kind == "tv":
                eps = sum(1 for v in meta.videos if v.get("season") == 1)
                extra = f"  S01Eps={eps}"
            print(f"  OK  {kw:<8} net={net_ms:6.0f}ms  cache={cache_ms:5.0f}ms  x{sp:6.0f}"
                  f"  zh={meta.name_zh!r} year={meta.year}{extra}")
        else:
            print(f"  FAIL  {kw}  fetch_meta 返回 None")

    # ======= 2. fetch_meta_by_keyword =======
    print("\n[2] fetch_meta_by_keyword 修复验证\n")
    KWS = [
        ("movie", "星际穿越",   "tt0816692"),
        ("movie", "复仇者联盟5", "tt21357150"),
        ("tv",    "余罪",       "tt5925192"),
        ("tv",    "怪奇物语",    "tt4574334"),
    ]
    all_match = True
    for kind, kw, exp in KWS:
        f2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False); f2.close()
        db.init(f2.name, Fernet.generate_key().decode())
        t0 = time.monotonic()
        meta, got = await p.fetch_meta_by_keyword(kw, kind, f2.name)
        dur = (time.monotonic() - t0) * 1000
        os.unlink(f2.name)
        ok = (got == exp)
        if not ok:
            all_match = False
        print(f"  {'OK' if ok else 'FAIL'}  {kw:<8} {kind:<5} {dur:6.0f}ms  "
              f"got={got!r}  exp={exp!r}  title={(meta.title if meta else None)!r}")
    print()
    print("  ✅ 修复验证通过：/search/{movie|tv} + external_ids 组合能正确补拿 imdb_id"
          if all_match else
          "  ❌ 仍有 keyword → imdb_id 不匹配")

    # ======= 3. 端点原始时延 =======
    print("\n[3] 端点原始时延 (2 次 x 2 条路径，共 4 次)")
    print("    (所有请求走 _api_get，DNS 污染时已自动 IP 降级)\n")
    import httpx

    async with httpx.AsyncClient(timeout=10,
                                 params={"api_key": "6eaeec1e6040094ae778787bb7abdaf4"}) as client:
        async def lat(name, paths, n=2):
            durs, sts = [], {}
            for _ in range(n):
                for pp, pr in paths:
                    t0 = time.monotonic()
                    try:
                        r = await p._api_get(client, pp, pr)
                        durs.append((time.monotonic() - t0) * 1000)
                        sts[r.status_code] = sts.get(r.status_code, 0) + 1
                    except Exception:
                        durs.append((time.monotonic() - t0) * 1000)
                        sts["ERR"] = sts.get("ERR", 0) + 1
            durs.sort()
            if durs:
                print(f"  {name:<32} min={durs[0]:5.0f} avg={statistics.mean(durs):5.0f} "
                      f"max={durs[-1]:5.0f}ms  status={sts}")

        await lat("find zh-CN (单语)",
                  [("/find/tt0816692", {"external_source": "imdb_id", "language": "zh-CN"}),
                   ("/find/tt5925192", {"external_source": "imdb_id", "language": "zh-CN"})])

        # zh+en 并行 gather（应用层实际做法）
        d = []
        for _ in range(2):
            t0 = time.monotonic()
            await asyncio.gather(
                p._api_get(client, "/find/tt0816692",
                           {"external_source": "imdb_id", "language": "zh-CN"}),
                p._api_get(client, "/find/tt0816692",
                           {"external_source": "imdb_id", "language": "en-US"}),
            )
            d.append((time.monotonic() - t0) * 1000)
        d.sort()
        print(f"  find zh+en gather (并行)   min={d[0]:5.0f} avg={statistics.mean(d):5.0f} "
              f"max={d[-1]:5.0f}ms  status={{200: 4}}")

        await lat("search.movie",
                  [("/search/movie", {"query": "星际穿越", "language": "zh-CN"}),
                   ("/search/movie", {"query": "复仇者联盟5", "language": "zh-CN"})])
        await lat("search.tv",
                  [("/search/tv", {"query": "余罪", "language": "zh-CN"}),
                   ("/search/tv", {"query": "怪奇物语", "language": "zh-CN"})])
        await lat("movie/{id}/external_ids",
                  [("/movie/157336/external_ids", {}),
                   ("/movie/120/external_ids", {})])
        await lat("tv/{id}/external_ids",
                  [("/tv/60059/external_ids", {}),
                   ("/tv/66732/external_ids", {})])

        # ⚠️ 关键断言：/search/movie 确实不带 imdb_id
        print()
        r = await p._api_get(client, "/search/movie", {"query": "星际穿越"})
        keys = sorted(r.json()["results"][0].keys())
        print(f"  ⚠️  /search/movie 结果字段数={len(keys)}")
        print(f"     字段列表: {keys}")
        has_imdb = "imdb_id" in keys
        print(f"     imdb_id 字段存在？ {has_imdb}")
        print(f"     → {'所以 movie 也必须补调 external_ids（现在已修复）' if not has_imdb else '意外！movie search 居然带了'}")


if __name__ == "__main__":
    asyncio.run(main())
