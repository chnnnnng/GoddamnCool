"""搜索链类：普通链 vs AI 链（后端一体化，前端无感）。

BaseSearchChain 直接用 candidate_service.search（内部已含 inspect_share），
补 canonical 字段；AISearchChain 在 Base 基础上追加 _ai_enrich（文件名合并
喂 AI → AI 返回元数据 → 服务端拼规范名 → 存 file_parses）。

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
    """普通搜索链：search（含 inspect_share）+ rule canonicalize。"""

    def __init__(self):
        from .candidate_routes import candidate_service
        self.service = candidate_service

    async def search(self, keyword: str, *, vtype: str = "movie", id: str = "",
                     season: int | None = None, disks=None, cookie: str = "") -> dict:
        svc = self.service
        meta, cands, errors, final_kw = await svc.search(
            vtype, id, keyword=keyword, disks=disks, cookie=cookie)
        # candidate_service.search 内部已调 inspect_share 填好 c.detail（含
        # video_count/files/seasons），这里只需给每个文件补 canonical 字段
        # （inspect_share 只算了 season/episode/spec，没算 canonical）
        for c in cands:
            self._rule_canonicalize(c, final_kw, getattr(c, "title", "") or "")
        return {"meta": meta, "cands": cands, "errors": errors, "keyword": final_kw}

    @staticmethod
    def _rule_canonicalize(cand, keyword: str, share_title: str) -> None:
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
        return await self.service.inspect_share(url, "", cookie=cookie)


class AISearchChain(BaseSearchChain):
    """AI 增强链：在 Base 基础上追加 _ai_enrich。"""

    async def search(self, keyword: str, *, vtype: str = "movie", id: str = "",
                     season: int | None = None, disks=None, cookie: str = "") -> dict:
        result = await super().search(keyword, vtype=vtype, id=id, season=season,
                                       disks=disks, cookie=cookie)
        meta = result["meta"]
        await self._ai_enrich(result["cands"], keyword=keyword,
                               title=(meta.title if meta else ""),
                               season=season)
        return result

    async def _ai_enrich(self, cands: list, *, keyword: str, title: str,
                         season: int | None) -> None:
        """文件名合并 → AI 精选+元数据解析 → 展开回填 → 存 file_parses。"""
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
            return
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
            # ai 标记挂在 detail dict 上（SearchCandidate 无 __setitem__）
            detail = getattr(cands[ci], "detail", None) or {}
            detail["ai"] = it
            setattr(cands[ci], "detail", detail)
            files_by = {pf["ci"]: pf for pf in (it.get("files") or [])}
            for compact_i, orig_indices in enumerate(mapping):
                pf = files_by.get(compact_i)
                if not pf:
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
                    f["canonical"] = canonical
                    f["canonical_source"] = "ai"
                    if ai_season is not None:
                        f["season"] = ai_season
                    if ai_episode is not None:
                        f["episode"] = ai_episode
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
                        source_key=(getattr(cands[ci], "share_url", "")
                                    or getattr(cands[ci], "title", "")[:100]),
                    )


def _aggregate_seasons(detail: dict) -> None:
    """重算季聚合（season/episode 更新后）。"""
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
