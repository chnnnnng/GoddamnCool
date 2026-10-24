"""影片-文件匹配器（MovieMatcher）。

从网盘文件名解析标题/年份/季集/分辨率，与 TMDB 元数据比对，
输出按置信度排序的候选。低置信度由调用方（阈值控制）决定是否返回。

验收要求（需求文档第 12 条）：
- 相似标题可匹配、不同年份不错误匹配、剧集必须集数正确
- 垃圾文件（预告/花絮/样片）不参与
- 置信度不足不返回流
"""
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from pypinyin import lazy_pinyin

from .tmdb import VideoMeta
from .providers.base import CloudFile

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".flv", ".webm", ".m2ts", ".mov", ".wmv", ".m4v"}

JUNK_PATTERN = re.compile(
    r"trailer|teaser|sample|extras?|featurette|behind[.\s_-]*the[.\s_-]*scenes|"
    r"花絮|预告|样片|广告",
    re.IGNORECASE,
)

# 文件名中标志"标题已结束"的元数据 token
_TOKEN_SPLIT = re.compile(r"[\s._\-\[\]()]+")
_META_TOKEN = re.compile(
    r"^(?:(?:19|20)\d{2}b?|s\d{1,2}e\d{1,3}|\d{1,3}x\d{1,3}|2160p?|1080p?|720p?|480p?|4k|"
    r"hdr10?|dolby|vision|dv|web-?dl|webrip|blu-?ray|bdrip|hdtv|dvdrip|remux|"
    r"x26[45]|h\.?26[45]|hevc|avc|aac|flac|dts|ac3|ddp?|truehd|atmos|10bit|8bit|60fps|chs|cht|mp4|mkv)$",
    re.IGNORECASE,
)
_YEAR_TOKEN = re.compile(r"(19|20)\d{2}")
_SEPE_TOKEN = re.compile(r"s(\d{1,2})e(\d{1,3})", re.IGNORECASE)
_EP_ONLY = re.compile(r"(?:ep?|第)\s*(\d{1,3})\s*(?:集|话|期)?", re.IGNORECASE)
_SEASON_CN = re.compile(r"第\s*([0-9一二三四五六七八九十百]+)\s*季")
_CN_NUM = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}
_CHINESE_EP = re.compile(r"第([零一二三四五六七八九十百]+)[集话期]")

_W = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


@dataclass
class ParsedName:
    raw: str
    title: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    resolution: str | None = None
    ext: str = ""


@dataclass
class MatchResult:
    file: CloudFile
    score: float = 0.0
    reason: str = ""
    parsed: ParsedName = field(default_factory=lambda: ParsedName(raw=""))


def _cn_to_int(s: str) -> int | None:
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, num = 0, 0
    for ch in s:
        d = _CN_NUM.get(ch)
        if d is None:
            return None
        if ch in "十百":
            total = (total + (num or 1)) * (10 if ch == "十" else 100)
            num = 0
        else:
            num = num * 10 + d
    return total + num


def _norm_title(s: str) -> str:
    return _W.sub("", s.lower())


def _title_keys(s: str) -> list[str]:
    """标题的多形态键：原样（去标点）+ 拼音（支持跨语言匹配，如 余罪 ↔ Yu Zui）。"""
    raw = _norm_title(s)
    if not raw:
        return []
    keys = [raw]
    py = _W.sub("", "".join(lazy_pinyin(s))).lower()
    if py and py != raw:
        keys.append(py)
    return keys


def _title_keys_similarity(file_title: str, meta_title: str) -> float:
    a_keys, b_keys = _title_keys(file_title), _title_keys(meta_title)
    best = 0.0
    for a in a_keys:
        for b in b_keys:
            if a == b:
                sim = 1.0
            elif a in b or b in a:
                sim = min(len(a), len(b)) / max(len(a), len(b))
            else:
                sim = SequenceMatcher(None, a, b).ratio()
            best = max(best, sim)
    return best


def parse(filename: str) -> ParsedName:
    """从文件名解析标题、年份、季集、分辨率。"""
    p = ParsedName(raw=filename)
    stem = filename
    if "." in stem and stem.rsplit(".", 1)[1].lower() in {e.lstrip(".") for e in VIDEO_EXTS}:
        stem, ext = stem.rsplit(".", 1)
        p.ext = "." + ext.lower()
    stem = re.sub(r"^\[.*?\]", "", stem)  # 去开头发布组标签 [GROUP]

    # 季集（S01E12 / 1x02 / 第x集 / 第x季）
    m = _SEPE_TOKEN.search(stem)
    if m:
        p.season, p.episode = int(m.group(1)), int(m.group(2))
    else:
        ms = _SEASON_CN.search(stem)
        if ms:
            p.season = _cn_to_int(ms.group(1)) or 1
        me = _CHINESE_EP.search(stem) or _EP_ONLY.search(stem)
        if me:
            p.episode = _cn_to_int(me.group(1))
        elif _EP_ONLY.search(stem):
            p.episode = _cn_to_int(_EP_ONLY.search(stem).group(1))

    # 分辨率
    mres = re.search(r"\b(2160p?|1080p?|720p?|480p?|4k)\b", stem, re.IGNORECASE)
    if mres:
        p.resolution = mres.group(1).lower()

    # 标题：首个元数据 token 之前的部分；无元数据 token 则取季集标记前
    tokens = [t for t in _TOKEN_SPLIT.split(stem) if t]
    title_tokens = []
    for i, t in enumerate(tokens):
        if _SEPE_TOKEN.fullmatch(t) or _SEASON_CN.fullmatch(t) or _EP_ONLY.fullmatch(t):
            break
        if _META_TOKEN.match(t):
            # 年份作为片名的特例：首个 token 是年份且下一个 token 也是年份（如《2012》）
            if i == 0 and _YEAR_TOKEN.fullmatch(t) and len(tokens) > 1 \
               and _YEAR_TOKEN.fullmatch(tokens[1] or ""):
                title_tokens.append(t)
                continue
            break
        title_tokens.append(t)
    p.title = " ".join(title_tokens).strip(" .-_")

    # 年份：取标题之后的首个年份 token（兼容片名本身是年份的特例，如《2012》）
    for t in tokens[len(title_tokens):]:
        if _YEAR_TOKEN.fullmatch(t):
            p.year = int(t)
            break
    return p


class MovieMatcher:
    """对 CloudFile 列表评分，返回置信度降序的候选。"""

    def __init__(self, threshold: float = 0.6):
        self.threshold = threshold

    # ---- 评分 ----

    def _title_similarity(self, file_title: str, meta_title: str) -> float:
        return _title_keys_similarity(file_title, meta_title)

    def match(self, files: list[CloudFile], meta: VideoMeta,
              season: int | None = None, episode: int | None = None) -> list[MatchResult]:
        results = []
        for f in files:
            if f.is_dir:
                continue
            p = parse(f.name)
            if p.ext not in VIDEO_EXTS:
                continue
            if JUNK_PATTERN.search(f.name):
                continue

            sim = self._title_similarity(p.title, meta.title)
            if sim < 0.5:
                continue

            # 年份硬否决：双方年份已知且差>=1 视为不同作品（需求：不同年份不错误匹配）
            if p.year and meta.year and abs(p.year - meta.year) >= 1:
                continue

            reason_parts = [f"标题相似度 {sim:.2f}"]
            score = sim * 0.7

            if meta.type == "series":
                # 集数是硬条件：文件无集数信息或集数不符均不参与
                if p.episode is None:
                    continue
                if episode is not None and p.episode != episode:
                    continue
                if season is not None and p.season is not None and p.season != season:
                    continue
                score += 0.3
                reason_parts.append(f"集数 S{p.season or 1}E{p.episode} 匹配")
            elif p.episode is not None:
                # 电影不应带集数标记（同名剧集片段），降权而非否决
                score -= 0.2
                reason_parts.append("电影带集数标记，已降权")

            results.append(MatchResult(
                file=f, score=round(min(score, 1.0), 3),
                reason="；".join(reason_parts)
                       + (f"；{p.resolution}" if p.resolution else ""),
                parsed=p,
            ))
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def best(self, files: list[CloudFile], meta: VideoMeta,
             season: int | None = None, episode: int | None = None) -> MatchResult | None:
        """返回唯一最佳候选；不足阈值或存在同分争议时返回 None（宁缺毋滥）。"""
        cands = self.match(files, meta, season, episode)
        if not cands:
            return None
        top = cands[0]
        if top.score < self.threshold:
            return None
        if len(cands) > 1 and cands[1].score == top.score and \
           cands[1].file.fid != top.file.fid:
            return None  # 同分歧义，避免错误播放
        return top
