"""影片-文件匹配器（MovieMatcher）。

从网盘文件名解析标题/年份/季集/分辨率，与 TMDB 元数据比对，
输出按置信度排序的候选。低置信度由调用方（阈值控制）决定是否返回。

验收要求（需求文档第 12 条）：
- 相似标题可匹配、不同年份不错误匹配、剧集必须集数正确
- 垃圾文件（预告/花絮/样片）不参与
- 置信度不足不返回流
"""
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from pypinyin import lazy_pinyin

from .name_parser import parser as name_parser
from .meta.base import VideoMeta
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


def rough_title_match(a, b, *, min_sim: float = 0.75) -> bool:
    """粗略影片名匹配（配合"精确文件大小匹配"使用）。

    转存文件会智能重命名为规范名，"已在网盘"不能再用同名判断；
    改用 精确大小（调用方负责）+ 粗略片名：片名相似（支持跨语言拼音）
    且季集不冲突即视为同一内容。a/b 可为文件名字符串或已解析对象
    （需含 title/title_en/season/episode 属性，如 name_parser.ParsedInfo）。"""
    pa = a if not isinstance(a, str) else name_parser.parse(a)
    pb = b if not isinstance(b, str) else name_parser.parse(b)
    ta, tb = pa.title or pa.title_en, pb.title or pb.title_en
    if not ta or not tb:
        return False
    if _title_keys_similarity(ta, tb) < min_sim:
        return False
    if pa.episode is not None and pb.episode is not None \
            and pa.episode != pb.episode:
        return False
    if pa.season is not None and pb.season is not None \
            and pa.season != pb.season:
        return False
    return True


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
        # 规范名可能是拼音/英文（如 "Yu.Zui.S02E05..."），meta 主名是中文（如"余罪"）：
        # 三个名字都参与相似度比较，避免跨语言匹配失败
        meta_titles = {t for t in (meta.title, meta.name_zh, meta.name_en) if t}
        for f in files:
            if f.is_dir:
                continue
            p = parse(f.name)
            if p.ext not in VIDEO_EXTS:
                continue
            if JUNK_PATTERN.search(f.name):
                continue

            sim = max(self._title_similarity(p.title, t) for t in meta_titles)
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


# ---- 文件名级补充解析（阶段 5：整包转存定位 / 候选详情规格展示） ----

_BARE_EP_RE = re.compile(r"^(?:EP?[-_. ]?)?(\d{1,3})$", re.IGNORECASE)


def episode_of(name: str) -> int | None:
    """裸集数文件名（如 02.mp4 / EP06.mkv）的集数提取；parse 失败时的补充手段。"""
    p = parse(name)
    if p.episode is not None:
        return p.episode
    stem = os.path.splitext(name)[0].strip()
    m = _BARE_EP_RE.match(stem)
    return int(m.group(1)) if m else None


_SPEC_RES_RE = re.compile(r"(?<!\d)(2160p?|1080[pi]|720p?|480p?|4k|uhd)(?!\d)", re.IGNORECASE)
_SPEC_SOURCE_RE = re.compile(r"(web-?dl|webrip|blu-?ray|bdrip|hdtv|dvdrip|remux)", re.IGNORECASE)
_SPEC_CODEC_RE = re.compile(r"(x26[45]|h\.?26[45]|hevc|avc|av1|xvid|divx)", re.IGNORECASE)
_SPEC_AUDIO_RE = re.compile(
    r"\b(aac|flac|ac-?3|e-?ac-?3|ddp|dd\+|dd|truehd|atmos|opus|dts(?:-?hd)?(?:[ .]ma)?)(?![a-z])"
    r"|(?<!\d)([257])\.([01])(?!\d)|(双声道|立体声)", re.IGNORECASE)
_SPEC_SUB_RE = re.compile(r"(简繁|简英|简体|繁体|中英双字|双语字幕|中字|内封|内嵌|外挂|chs|cht)",
                          re.IGNORECASE)
_SPEC_LANG_RE = re.compile(r"(国粤双语|普通话|国语|粤语|日语原声|英语原声|韩语原声|双语)")

_SPEC_AUDIO_MAP = {"aac": "AAC", "flac": "FLAC", "ac3": "AC3", "eac3": "E-AC3",
                   "ddp": "DD+", "dd+": "DD+", "dd": "DD", "truehd": "TrueHD",
                   "atmos": "Atmos", "dts": "DTS", "dtshd": "DTS-HD",
                   "dtshdma": "DTS-HD MA", "opus": "Opus", "51": "5.1", "71": "7.1",
                   "20": "2.0", "双声道": "双声道", "立体声": "立体声"}


def parse_file_spec(filename: str) -> dict:
    """从文件名提取展示规格：分辨率/来源/编码/音轨/字幕/语言（缺省字段省略）。

    仅做文件名启发式标注（网盘文件无法读取媒体流信息），供候选详情参考。
    """
    spec: dict = {}
    m = _SPEC_RES_RE.search(filename)
    if m:
        r = m.group(1).lower()
        spec["resolution"] = "4K" if r in ("2160p", "2160", "4k", "uhd") else r
    m = _SPEC_SOURCE_RE.search(filename)
    if m:
        s = m.group(1).lower().replace("-", "")
        spec["source"] = {"webdl": "WEB-DL", "webrip": "WEBRip", "bluray": "BluRay",
                          "bdrip": "BluRay", "hdtv": "HDTV", "dvdrip": "DVDRip",
                          "remux": "Remux"}.get(s, s.upper())
    m = _SPEC_CODEC_RE.search(filename)
    if m:
        c = m.group(1).lower().replace(".", "")
        spec["codec"] = {"x265": "H.265", "h265": "H.265", "hevc": "H.265",
                         "x264": "H.264", "h264": "H.264", "avc": "H.264",
                         "av1": "AV1", "xvid": "XviD", "divx": "DivX"}.get(c, c.upper())
    audio = []
    for m in _SPEC_AUDIO_RE.finditer(filename):
        a = re.sub(r"[-. ]", "", m.group(0)).lower()
        audio.append(_SPEC_AUDIO_MAP.get(a, a.upper()))
    audio = list(dict.fromkeys(audio))
    if audio:
        spec["audio"] = audio
    subs = [{"chs": "简体", "cht": "繁体"}.get(m.group(0).lower(), m.group(0))
            for m in _SPEC_SUB_RE.finditer(filename)]
    subs = list(dict.fromkeys(subs))
    if subs:
        spec["subtitle"] = subs
    langs = list(dict.fromkeys(_SPEC_LANG_RE.findall(filename)))
    if langs:
        spec["languages"] = langs
    return spec
