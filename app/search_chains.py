"""搜索链类：普通链 vs AI 链（后端一体化，前端无感）。

普通链（NormalSearchChain）：PanSou 搜索 → 分享详情 inspect → 用户已有内容匹配。
AI 链（AISearchChain）：同上 + 文件名合并喂 AI → AI 返回结构化元数据 →
服务端 name_parser.canonical 拼规范名 → 存 file_parses 解析资料库。

candidate_routes.py 只根据 llm.configured() 选链实例，前端零改动。"""
from __future__ import annotations

import os

from . import config, db, llm, settings
from .name_parser import canonical_with_context, parser as name_parser


def _folder_of(path: str) -> str:
    """取文件所在目录名（"第三季/06.mp4" → "第三季"）。"""
    parent = path.rstrip("/").rsplit("/", 1)[0] if "/" in path.rstrip("/") else ""
    return parent.rsplit("/", 1)[-1] if parent else ""


class BaseSearchChain:
    """普通搜索链：search → inspect → exists，不含 AI 增强。"""

    def __init__(self):
        # 延迟 import candidate_routes.candidate_service 避免循环依赖
        from .candidate_routes import candidate_service
        self.service = candidate_service

    # ---- search/inspect/exists 三步在 Normal/AI 下逻辑相同 ----
    # AI 链只重写 _post_process_candidates / _post_process_inspect 钩子

    async def search(self, keyword: str, *, vtype: str = "movie", id: str = "",
                     season: int | None = None, disks=None, cookie: str = "") -> dict:
        svc = self.service
        meta, cands, errors, final_kw = await svc.search(
            vtype, id, keyword=keyword, disks=disks, cookie=cookie)
        # inspect 每个候选拿详情（files/total_size）
        for c in cands:
            if getattr(c, "inspected", False):
                continue
            try:
                detail = await svc.inspect(c.source, c.url, cookie=cookie)
                if detail:
                    c.detail = detail
                    c.inspected = True
                    # Normal 链：用规则解析算 canonical（AI 链会用 AI 元数据覆盖）
                    self._rule_canonicalize(c, final_kw, getattr(c, "title", "") or "")
            except Exception:
                c.detail = None
        return {"meta": meta, "cands": cands, "errors": errors, "keyword": final_kw}

    @staticmethod
    def _rule_canonicalize(cand, keyword: str, share_title: str) -> None:
        """Normal 链：用 name_parser 规则解析算每个文件的 canonical。"""
        d = getattr(cand, "detail", None)
        if not d or not (d.get("files")):
            return
        for f in d["files"]:
            nm = f.get("name") or ""
            f.setdefault("canonical", canonical_with_context(
                nm, search_title=keyword or "",
                share_title=share_title,
                folder=_folder_of(f.get("path") or "")))

    async def inspect(self, source: str, url: str, *, cookie: str = "") -> dict | None:
        return await self.service.inspect(source, url, cookie=cookie)


class AISearchChain(BaseSearchChain):
    """AI 增强链：search 后做文件名合并 → AI 精选+元数据解析 → 展开回填
    → 服务端拼规范名 → 存 file_parses 资料库。"""

    async def search(self, keyword: str, *, vtype: str = "movie", id: str = "",
                     season: int | None = None, disks=None, cookie: str = "") -> dict:
        result = await super().search(keyword, vtype=vtype, id=id, season=season,
                                       disks=disks, cookie=cookie)
        await self._ai_enrich(result["cands"], keyword=keyword,
                               title=(result["meta"].title if result["meta"] else ""),
                               season=season, cookie=cookie)
        return result

    async def inspect(self, source: str, url: str, *, cookie: str = "") -> dict | None:
        detail = await super().inspect(source, url, cookie=cookie)
        if detail:
            self._record_inspect_parses(detail.get("files") or [])
        return detail

    async def _ai_enrich(self, cands: list, *, keyword: str, title: str,
                         season: int | None, cookie: str) -> None:
        """对 cands 做 AI 精选+文件名合并解析（需求第 1/2/4 条）。

        入参 cands 是 SearchCandidate 对象列表（candidate_service 返回 dataclass），
        但 .detail 字段是 dict（文件列表/规格等）。SearchCandidate 本身用属性访问，
        detail 内部 dict 用 .get()。回填 ai 标记到 c.detail（dict 字段）。"""
        if not llm.configured() or not cands:
            return
        VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".ts",
                     ".m2ts", ".mpg", ".mpeg", ".m4v", ".iso"}
        compact_per_cand: list[tuple[list[str], list[list[int]], list[dict]]] = []
        send_cands: list[dict] = []
        for c in cands:
            d = getattr(c, "detail", None) or {}
            files = d.get("files") or []
            vf = [f for f in files
                  if os.path.splitext((f.get("path") or f.get("name") or ""))[1].lower() in VIDEO_EXT]
            vf = vf[:40]
            paths = [f.get("path") or f.get("name") or "" for f in vf]
            compact, mapping = llm.compact_file_paths(paths)
            compact_per_cand.append((compact, mapping, vf))
            send_cands.append({
                "title": getattr(c, "title", None),
                "source": getattr(c, "source", None),
                "published": getattr(c, "published", None),
                "alive_state": getattr(c, "alive_state", None),
                "video_count": d.get("video_count"),
                "total_size_h": d.get("total_size_h"),
                "files": [{"path": p} for p in compact],
            })
        if not any(sp["files"] for sp in send_cands):
            return  # 没有候选带 files → 不调 AI（省 token）
        try:
            items = await llm.review_candidates(
                send_cands, keyword=keyword, title=title, season=season)
        except llm.LLMError as e:
            print(f"[AISearchChain] AI enrich 失败: {e}")
            return
        by = {it["index"]: it for it in items}
        for ci, (compact, mapping, vf) in enumerate(compact_per_cand):
            it = by.get(ci)
            if not it:
                continue
            # SearchCandidate 本身无 __setitem__，ai 标记挂在其 detail dict 上
            detail = getattr(cands[ci], "detail", None) or {}
            detail["ai"] = it
            setattr(cands[ci], "detail", detail)
            files_by = {pf["ci"]: pf for pf in (it.get("files") or [])}
            for compact_i, orig_indices in enumerate(mapping):
                pf = files_by.get(compact_i)
                if not pf:
                    continue
                # 展开到每个原始文件
                for orig_i in orig_indices:
                    if orig_i >= len(vf):
                        continue
                    f = vf[orig_i]
                    orig_name = os.path.basename(f.get("path") or f.get("name") or "")
                    info = name_parser.parse(orig_name)
                    # 元数据：AI 给的 + parse 原有的（AI 没给的用 parse 的）
                    ai_title = pf.get("title") or info.title or info.title_en
                    ai_season = pf.get("season") if pf.get("season") is not None else info.season
                    ai_episode = pf.get("episode") if pf.get("episode") is not None else info.episode
                    ai_year = pf.get("year") if pf.get("year") is not None else info.year
                    # 拼规范名（需求第 2 条：服务端用元数据拼，不用 AI 直接吐）
                    canonical = name_parser.canonical(
                        info, title=ai_title, season=ai_season,
                        episode=ai_episode, year=ai_year)
                    # 回填到文件对象
                    f["canonical"] = canonical
                    f["canonical_source"] = "ai"
                    if ai_season is not None:
                        f["season"] = ai_season
                    if ai_episode is not None:
                        f["episode"] = ai_episode
                    # 存 file_parses（需求第 4 条）
                    structured = {
                        "title": ai_title, "year": ai_year,
                        "season": ai_season, "episode": ai_episode,
                        "resolution": pf.get("resolution") or info.resolution,
                        "source": pf.get("source") or info.source,
                        "codec": pf.get("codec") or info.codec,
                        "audio": pf.get("audio") or (info.audio[0] if info.audio else None),
                        "group": pf.get("group") or (info.group[0] if info.group else None),
                    }
                    db.record_file_parse(
                        config.DB_PATH,
                        original_path=f.get("path") or f.get("name") or "",
                        original_name=orig_name,
                        parse_method="ai",
                        structured_info=structured,
                        canonical_name=canonical,
                        source_scene="ai_review",
                        source_key=cands[ci].get("url") or cands[ci].get("title", "")[:100],
                    )
            # 重算季聚合（season/episode 已更新）
            detail = cands[ci].get("detail")
            if detail and (detail.get("files") or []):
                _aggregate_seasons(detail)
                cands[ci]["detail"] = detail

    def _record_inspect_parses(self, files: list[dict]) -> None:
        """inspect 时存 rule 解析的 file_parses（需求第 4 条）。"""
        for f in files[:80]:
            path = f.get("path") or f.get("name") or ""
            if not path:
                continue
            orig_name = os.path.basename(path)
            info = name_parser.parse(orig_name)
            if not (info.title or info.title_en):
                continue  # 规则解不出标题 → 等 AI
            canonical = canonical_with_context(orig_name)
            structured = {
                "title": info.title or info.title_en, "year": info.year,
                "season": info.season, "episode": info.episode,
                "resolution": info.resolution, "source": info.source,
                "codec": info.codec,
                "audio": info.audio[0] if info.audio else None,
                "group": info.group[0] if info.group else None,
            }
            db.record_file_parse(
                config.DB_PATH,
                original_path=path, original_name=orig_name,
                parse_method="rule", structured_info=structured,
                canonical_name=canonical, source_scene="inspect",
            )


def _aggregate_seasons(detail: dict) -> None:
    """重算季聚合（season/episode 更新后）。"""
    from .candidate_routes import _format_season_chip
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
        detail["season_chips"] = [_format_season_chip(s) for s in seasons]
