"""搜索链：普通链 + AI 链。

普通链（BaseSearchChain.search）完整流程：
  1. PanSou API 搜索 → verify_links 初步分档（ok/bad/uncertain/passcode）
  2. alive 候选按匹配度 score 降序
  3. 取 TOP INSPECT_TOP_N → inspect_share 普通解析（填 detail.files/video_count/...）
  4. 二次筛选：video_count=0 或 total_size_h < MIN_VIDEO_SIZE_H → alive_state="uncertain"（存疑）
  5. 剩余 alive 候选按"规格综合分"排序（清晰度 + 集数 + 体积 + 匹配度）
  6. 所有候选补规则 canonical（兜底）
  7. 返回

AI 链（AISearchChain.search）在普通链**之后**叠加：
  1. 普通链跑完（候选已排序 + 已解析）
  2. 从最终 alive 候选里取 LLM_SKIM_TOP_N
  3. SKIM 概要甄选（keep=true/false + score + reason）
  4. 从 keep=true 里按 AI 评分降序取 LLM_DEEP_TOP_N
  5. DEEP PARSE 批量解析（标题+英文名+季集+清晰度+来源+编码+音轨+发布组）
  6. **替换**：AI 元数据 100% 覆盖 detail.files 里的原普通解析字段
     → canonical 用 AI 元数据重新拼（带 canonical_source="ai"）
  7. 返回

candidate_routes.py 只根据 llm.configured() 选链实例，前端零改动。"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from . import config, db, llm, settings
from .name_parser import canonical_with_context, parser as name_parser

log = logging.getLogger(__name__)

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".ts",
             ".m2ts", ".mpg", ".mpeg", ".m4v", ".iso"}


def _is_alive(c) -> bool:
    """判定候选是否属于"有效"档。

    - alive_state in ("ok", "passcode", "locked") → 明确 alive
    - alive_state 为空/None → 也视为 alive（非 quark 链接跳过了 verify_links，
      不是真的"无效"，只是没被检测过）
    - alive_state in ("bad", "uncertain", "need_code", "unsupported") → 不算
    """
    st = c.alive_state
    if not st:
        return True
    return st in ("ok", "passcode", "locked")


def _human_size_to_float_h(val) -> float:
    """把 _human_size 产生的 "44.2GB" / "108.5MB" / "0B" 等转成 float GB。"""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().upper()
    m = re.match(r"([\d.]+)\s*([KMGT]?B?)", s)
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = m.group(2)
    mult = {"": 1.0, "B": 1/(1024**3), "K": 1/(1024**2), "KB": 1/(1024**2),
            "M": 1/1024, "MB": 1/1024, "G": 1.0, "GB": 1.0, "T": 1024.0, "TB": 1024.0}
    return num * mult.get(unit, 1.0)


def _folder_of(path: str) -> str:
    parent = path.rstrip("/").rsplit("/", 1)[0] if "/" in path.rstrip("/") else ""
    return parent.rsplit("/", 1)[-1] if parent else ""


class BaseSearchChain:
    """普通搜索链。"""

    def __init__(self):
        from .candidate_routes import candidate_service
        self.service = candidate_service

    # ==================== 普通链主流程 ====================

    async def search(self, keyword: str, *, vtype: str = "movie", id: str = "",
                     season: int | None = None, disks=None, cookie: str = "") -> dict:
        svc = self.service
        cache_key = self._cache_key(vtype, id, keyword, disks)
        _TTL = float(settings.get("SEARCH_CACHE_TTL") or config.SEARCH_CACHE_TTL)
        # ---- 统一缓存：ttl=0 关闭；否则优先 AI 增强缓存，再普通缓存 ----
        if _TTL > 0:
            cached = db.get_chain_cache(config.DB_PATH, cache_key)
            if cached and cached.get("ai_enhanced"):
                log.info("[chain] 命中 AI 增强缓存 %s", cache_key)
                return self._restore(cached["data"])
            if cached:
                log.info("[chain] 命中普通缓存 %s", cache_key)
                return self._restore(cached["data"])

        # ---- 阶段 A：PanSou 搜索 + verify_links 初步分档 ----
        meta, cands, errors, final_kw = await svc.search(
            vtype, id, keyword=keyword, disks=disks, cookie=cookie)

        result = await self._finish(meta, cands, errors, final_kw, cookie=cookie)

        # ---- 写缓存（普通）----
        if _TTL > 0:
            db.set_chain_cache(config.DB_PATH, cache_key, self._serialized(result),
                               ai_enhanced=False, ttl=_TTL)
        return result

    # ------------------------------ 内部 helpers ------------------------------

    @staticmethod
    def _cache_key(vtype: str, video_id: str, keyword: str, disks) -> str:
        disks_key = "all" if disks == [] else (",".join(disks) if disks else "default")
        return f"search:{vtype}|{video_id or ''}|{keyword}|{disks_key}"

    @staticmethod
    def _serialized(result: dict) -> dict:
        from .candidate_service import CandidateService as _CS
        meta = result["meta"]
        return {
            "meta": ({"imdb_id": getattr(meta, "id", None),
                      "name": getattr(meta, "title", None),
                      "name_zh": getattr(meta, "name_zh", None),
                      "name_en": getattr(meta, "name_en", None),
                      "type": getattr(meta, "type", None),
                      "year": getattr(meta, "year", None),
                      "videos": getattr(meta, "videos", None)} if meta else None),
            "candidates": [_CS._cand_dict(c) for c in result["cands"]],
            "errors": result.get("errors", []),
            "kw": result["keyword"],
        }

    @staticmethod
    def _restore(data: dict) -> dict:
        from .candidate_service import CandidateService as _CS, SearchCandidate
        from .tmdb import VideoMeta
        meta_d = data.get("meta")
        meta = VideoMeta(meta_d) if meta_d else None
        cands = [SearchCandidate(**c) for c in data.get("candidates", [])]
        return {"meta": meta, "cands": cands,
                "errors": data.get("errors", []),
                "keyword": data.get("kw") or ""}

    async def _finish(self, meta, cands, errors, final_kw, *, cookie: str) -> dict:
        """普通链的阶段 B~F：inspect → 二次筛选 → 规格排序 → canonical → 分档。"""
        svc = self.service

        # ---- 阶段 B：TOP INSPECT_TOP_N alive 候选做 inspect_share ----
        alive = [c for c in cands if _is_alive(c)]
        alive.sort(key=lambda c: -c.score)
        _INSPECT_N = max(3, int(settings.get("INSPECT_TOP_N")
                                or settings.get("INSPECT_LIMIT")
                                or config.INSPECT_TOP_N))
        targets = alive[:_INSPECT_N]
        if targets and cookie:
            sem = asyncio.Semaphore(4)

            async def _one(c):
                async with sem:
                    try:
                        c.detail = await svc.inspect_share(
                            c.share_url, c.passcode, cookie=cookie)
                    except Exception as e:
                        log.info("候选内容解析失败 %s: %s", c.share_url, e)

            await asyncio.gather(*(_one(c) for c in targets))

        # ---- 阶段 C：二次筛选 → 存疑 ----
        _MIN_SIZE_H = float(settings.get("MIN_VIDEO_SIZE_H") or config.MIN_VIDEO_SIZE_H)
        for c in targets:
            d = getattr(c, "detail", None)
            if not d:
                continue
            vc = d.get("video_count", 0) or 0
            ts = _human_size_to_float_h(d.get("total_size_h"))
            if vc == 0:
                c.alive_state = "uncertain"
                c.alive_summary = "内容无视频文件"
            elif 0 < _MIN_SIZE_H and 0 < ts < _MIN_SIZE_H:
                c.alive_state = "uncertain"
                c.alive_summary = f"内容体积过小（< {_MIN_SIZE_H}GB）"

        # ---- 阶段 D：规格排序 + 未 inspect 保持匹配度顺序 ----
        inspected_ok = []
        uninspected_alive = []
        for c in alive:
            if getattr(c, "detail", None) and _is_alive(c):
                inspected_ok.append(c)
            elif _is_alive(c):
                uninspected_alive.append(c)
        inspected_ok.sort(key=self._spec_score, reverse=True)

        # ---- 阶段 E：所有候选补规则 canonical ----
        for c in cands:
            self._rule_canonicalize(c, final_kw, getattr(c, "title", "") or "")

        # ---- 阶段 F：最终分档 ----
        bad = [c for c in cands if c.alive_state in ("bad", "unsupported")]
        uncertain = [c for c in cands if c.alive_state == "uncertain"]
        ordered = inspected_ok + uninspected_alive + uncertain + bad

        cands.clear()
        cands.extend(ordered)
        return {"meta": meta, "cands": cands, "errors": errors, "keyword": final_kw}

    @staticmethod
    def _spec_score(c) -> float:
        """规格综合分：清晰度 + 集数 + 体积 + 匹配度。"""
        d = getattr(c, "detail", None) or {}
        vc = d.get("video_count") or 0
        ts = _human_size_to_float_h(d.get("total_size_h"))
        # 清晰度（从文件里取最高）
        res_rank = {"4320p": 5, "2160p": 4, "1080p": 3, "720p": 2, "480p": 1}
        best_res = 0
        for f in (d.get("files") or []):
            r = (f.get("resolution") or "").lower()
            if r in res_rank:
                best_res = max(best_res, res_rank[r])
        # 匹配度 score（PanSou 返回的原始匹配度）
        base = getattr(c, "score", 0) or 0
        return base * 10 + best_res * 20 + min(vc, 100) + min(ts, 500) * 0.1

    @staticmethod
    def _rule_canonicalize(cand, keyword: str, share_title: str) -> None:
        d = getattr(cand, "detail", None)
        if not d or not (d.get("files")):
            return
        for f in d["files"]:
            if f.get("canonical"):  # AI 已经填过的不覆盖
                continue
            nm = f.get("name") or ""
            f["canonical"] = canonical_with_context(
                nm, search_title=keyword or "",
                share_title=share_title,
                folder=_folder_of(f.get("path") or ""))

    async def inspect(self, source: str, url: str, *, cookie: str = "") -> dict | None:
        return await self.service.inspect_share(url, "", cookie=cookie)


class AISearchChain(BaseSearchChain):
    """AI 精选链：缓存优先命中 AI 结果 → 否则增量（普通缓存之上叠加 AI）→ 全 miss 跑普通再 AI。"""

    async def search(self, keyword: str, *, vtype: str = "movie", id: str = "",
                     season: int | None = None, disks=None, cookie: str = "") -> dict:
        cache_key = self._cache_key(vtype, id, keyword, disks)

        # ---- 统一缓存：先看有没有 AI 结果 ----
        cached = db.get_chain_cache(config.DB_PATH, cache_key)
        if cached:
            if cached.get("ai_enhanced"):
                log.info("[AI chain] 命中 AI 增强缓存 %s", cache_key)
                return self._restore(cached["data"])
            # 命中普通缓存——在其基础上增量跑 AI 链，不重跑普通搜索
            log.info("[AI chain] 命中普通缓存，增量跑 AI %s", cache_key)
            result = self._restore(cached["data"])
        else:
            # ---- 全 miss：先跑普通链 ----
            result = await super().search(keyword, vtype=vtype, id=id,
                                          season=season, disks=disks, cookie=cookie)

        meta = result["meta"]
        # ---- 增量叠加 AI 链 ----
        await self._ai_enrich(result["cands"], keyword=keyword,
                               title=(meta.title if meta else ""),
                               season=season)

        # ---- 写 AI 增强缓存（同时覆盖普通缓存，用户明确升级）----
        db.set_chain_cache(config.DB_PATH, cache_key, self._serialized(result),
                           ai_enhanced=True,
                           ttl=float(settings.get("SEARCH_CACHE_TTL")
                                     or config.SEARCH_CACHE_TTL))
        return result

    async def _ai_enrich(self, cands: list, *, keyword: str, title: str,
                         season: int | None) -> None:
        """两阶段 AI：SKIM 概要甄选 → DEEP PARSE 批量解析 → **替换**普通解析结果。"""
        if not llm.configured() or not cands:
            return

        # ---- 0. SKIM 只针对"被 inspect 过 + 通过二次筛选"的候选 ----
        # 普通链已把 inspected_ok 放在最前面（规格综合分排序），
        # 然后是 uninspected_alive（匹配度顺序），uncertain/bad 在最后。
        # SKIM 只挑 inspected_ok 那批，uninspected 的不参与 AI 甄选
        inspected_alive = [c for c in cands
                           if _is_alive(c) and getattr(c, "detail", None)]
        if not inspected_alive:
            return
        _SKIM_N = max(3, int(settings.get("LLM_SKIM_TOP_N") or config.LLM_SKIM_TOP_N))
        alive_top = inspected_alive[:_SKIM_N]  # 已是规格排序后的 TOP N

        # 预处理：每个候选的 vf / paths / LCP 压缩
        pre: list[dict] = []
        for ci, c in enumerate(cands):
            d = getattr(c, "detail", None) or {}
            files = d.get("files") or []
            vf = [f for f in files
                  if os.path.splitext((f.get("path") or f.get("name") or ""))[1].lower() in VIDEO_EXT]
            paths = [f.get("path") or f.get("name") or "" for f in vf]
            compact, mapping = llm.compact_file_paths_lcp(paths)
            pre.append({
                "ci": ci,
                "vf": vf, "paths": paths,
                "compact": compact, "mapping": mapping,
            })

        # alive_top 在 cands 里的下标
        alive_indices = [cands.index(c) for c in alive_top]

        # ---- 阶段 1: SKIM（概要甄选）----
        summary = []
        for ci in alive_indices:
            p = pre[ci]
            d = getattr(cands[ci], "detail", None) or {}
            summary.append({
                "title": getattr(cands[ci], "title", None),
                "video_count": d.get("video_count"),
                "total_size_h": d.get("total_size_h"),
                "files_sample": p["paths"][:3],
            })
        try:
            skims = await llm.skim_candidates(
                summary, keyword=keyword, title=title, season=season)
        except llm.LLMError as e:
            log.info("[AISearchChain] SKIM 失败: %s", e)
            return

        # 挂 ai 标记 + 挑 keep=true 的进 DEEP
        keep_pairs: list[tuple[int, int]] = []  # (alive_top 里的位置, AI score)
        for send_idx, sk in enumerate(skims):
            ci = alive_indices[send_idx]
            detail = getattr(cands[ci], "detail", None) or {}
            detail["ai"] = {
                "keep": sk["keep"], "score": sk["score"],
                "reason": sk["reason"], "title": sk["title"],
            }
            setattr(cands[ci], "detail", detail)
            if sk["keep"]:
                sc = sk["score"] if sk["score"] is not None else 50
                keep_pairs.append((send_idx, sc))

        _DEEP_N = max(1, int(settings.get("LLM_DEEP_TOP_N") or config.LLM_DEEP_TOP_N))
        keep_pairs.sort(key=lambda x: -x[1])
        top_pairs = keep_pairs[:_DEEP_N]
        if not top_pairs:
            return

        # ---- 阶段 2: DEEP PARSE（批量）----
        deep_send: list[dict] = []
        deep_meta: list[tuple[int, int]] = []  # (deep_idx, cands 下标)
        for deep_send_i, (send_idx, _sc) in enumerate(top_pairs):
            ci = alive_indices[send_idx]
            deep_send.append({
                "title": getattr(cands[ci], "title", None),
                "paths": pre[ci]["compact"],
            })
            deep_meta.append((deep_send_i, ci))
        try:
            deep_results = await llm.parse_candidates_deep(
                deep_send, keyword=keyword, title=title, season=season)
        except llm.LLMError as e:
            log.info("[AISearchChain] DEEP PARSE 失败: %s", e)
            return

        # ---- 阶段 3: 替换 ----
        for deep_item in deep_results:
            deep_i = deep_item["index"]
            if deep_i >= len(deep_meta):
                continue
            _, ci = deep_meta[deep_i]
            p = pre[ci]
            pf_by = {pf["ci"]: pf for pf in deep_item.get("files", [])}
            for compact_i, orig_indices in enumerate(p["mapping"]):
                pf = pf_by.get(compact_i)
                if not pf:
                    continue
                for orig_i in orig_indices:
                    if orig_i >= len(p["vf"]):
                        continue
                    self._apply_ai_parse(p["vf"][orig_i], pf, cands[ci], title)

    def _apply_ai_parse(self, f: dict, pf: dict, cand, default_title: str) -> None:
        """**替换**：AI 元数据 100% 覆盖 detail.files[i] 的原普通解析字段。"""
        orig_name = os.path.basename(f.get("path") or f.get("name") or "")
        info = name_parser.parse(orig_name)

        # AI 元数据（全部覆盖，season/episode 仅在 AI 提供时覆盖）
        ai_title = pf.get("title") or info.title or info.title_en or default_title
        ai_title_en = pf.get("title_en") or info.title_en
        ai_season = pf.get("season") if pf.get("season") is not None else info.season
        ai_episode = pf.get("episode") if pf.get("episode") is not None else info.episode
        ai_year = pf.get("year") if pf.get("year") is not None else info.year

        # 用 AI 规格覆盖 info，让 canonical 自动带上
        # resolution 走归一化（4K→2160p, 8K→4320p 等）避免 AI 和 parser 产出不一致
        ai_res_raw = pf.get("resolution")
        if ai_res_raw:
            from .name_parser import normalize_resolution
            info.resolution = normalize_resolution(ai_res_raw) or info.resolution
        info.source = pf.get("source") or info.source
        ai_codec = pf.get("codec") or ""
        ai_audio = pf.get("audio") or ""
        ai_grp = pf.get("group") or ""
        if ai_codec:
            info.codec = ai_codec
        if ai_audio:
            info.audio = [ai_audio]
        if ai_grp:
            info.group = ai_grp

        canonical = name_parser.canonical(
            info, title=ai_title, title_en=ai_title_en,
            season=ai_season, episode=ai_episode, year=ai_year)

        # 全部替换
        f["canonical"] = canonical
        f["canonical_source"] = "ai"
        f["title"] = ai_title
        if ai_title_en:
            f["title_en"] = ai_title_en
        if ai_season is not None:
            f["season"] = ai_season
        if ai_episode is not None:
            f["episode"] = ai_episode
        if ai_year is not None:
            f["year"] = ai_year
        # 规格字段
        if info.resolution:
            f["resolution"] = info.resolution
        if info.source:
            f["source"] = info.source
        if info.codec:
            f["codec"] = info.codec
        if ai_audio:
            f["audio"] = ai_audio
        if ai_grp:
            f["group"] = ai_grp

        db.record_file_parse(
            config.DB_PATH,
            original_path=f.get("path") or f.get("name") or "",
            original_name=orig_name,
            parse_method="ai",
            structured_info={
                "title": ai_title, "title_en": ai_title_en,
                "year": ai_year, "season": ai_season, "episode": ai_episode,
                "resolution": info.resolution, "source": info.source,
                "codec": info.codec, "audio": ai_audio, "group": ai_grp,
            },
            canonical_name=canonical,
            source_scene="ai_review",
            source_key=(getattr(cand, "share_url", "")
                        or getattr(cand, "title", "")[:100]),
        )


def _aggregate_seasons(detail: dict) -> None:
    files = detail.get("files") or []
    by_season: dict[int, list[dict]] = {}
    for f in files:
        s = f.get("season")
        if s is None:
            continue
        by_season.setdefault(int(s), []).append(f)
    seasons = []
    for s in sorted(by_season):
        fs = by_season[s]
        eps = sorted({e.get("episode") for e in fs if e.get("episode") is not None})
        sizes = [int(f.get("size") or 0) for f in fs if f.get("size")]
        seasons.append({
            "season": s,
            "episodes": len(eps),
            "size_h": round(sum(sizes) / 1024**3, 2) if sizes else 0,
        })
    if seasons:
        detail["seasons"] = seasons
