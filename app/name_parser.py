"""乱七八糟影视资源文件名解析器（MessyNameParser）。

针对网盘分享中千奇百怪的命名（样本库 tests/data/messy_filenames.txt，
1039 个真实文件名），尽量正确提取结构化信息，并输出规范文件名。

覆盖的命名模式（按样本归纳）：
- 季集标注：S01E01 / S01.E01 / SE01.01 / 1x01 / E01 / EP001 / 第X集 / 第X季 /
  第五季第一集 / 纯数字（001、1001）/ E01-E03 范围 / 无季集（电影）
- 标题形态：中文点号、英文点号、中英双语（后宫·甄嬛传.Empresses.in.the.Palace）、
  空格式（Breaking Bad ... S01E04 - 第 4 集）、全角符号（【】（）·~×）
- 干扰项：【新增】等前缀、Top013. 等编号前缀、发布组混入标题尾（-年糕 @一番）、
  敏感词改写（绝-命-D师）、纯编号名（Z99546-2024-2160p）、假年份（火影2016p）
- 规格：4K/2160p/HD2160P/4KHDR、WEB-DL/BluRay/REMUX/BDrip、H.264/x265/×265、
  AAC5.1/DDP5.1/TrueHD.7.1/Atmos/DTS-HD.MA、CHS-ENG/中英双字/官方中字、
  国粤双语/HDR/DV/60fps/10bit

canonical() 输出规范名（点号风格，Stremio/媒体库友好）：
  电影 Title.Year.Resolution.Source.Codec.Audio-Group.ext
  剧集 Title.SxxEyy.Resolution.Source.Codec.Audio-Group.ext
"""
import os
import re
from dataclasses import dataclass, field

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".flv", ".webm", ".m2ts", ".mov",
              ".wmv", ".m4v", ".iso", ".wmv"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub"}

JUNK_RE = re.compile(
    r"trailer|teaser|sample|extras?|featurette|behind[.\s_-]*the[.\s_-]*scenes|"
    r"花絮|预告|样片|广告", re.IGNORECASE)

# ---- 分词与提取用的正则 ----
_TOKEN_SPLIT = re.compile(r"[\s._\-~×x*]+")  # ×/x 分隔（HD2160P×265）
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u00b7\uff08\uff09\u3010\u3011]")
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?![\dpPiIbB])")  # 排除 2016p/1080p
_BRACKETS_RE = re.compile(r"[【\[]([^【】\[\]]{1,40})[】\]]")

# 季集：按特异性降序（先长后短，避免 S01 吃掉 S01E01）
_SEEP_RE = re.compile(r"(?i)\bS(\d{1,2})[.\-_ ]?E(\d{1,4})(?:\s?-?\s?E(\d{1,4}))?\b")
_SE_DOT_EP_RE = re.compile(r"(?i)\bSE(\d{1,2})[.\-_ ](\d{1,4})\b")        # SE01.01
_NXN_RE = re.compile(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)", re.IGNORECASE)  # 1x01
_S_ONLY_RE = re.compile(r"(?i)\bS(\d{1,2})\b(?![.\-_ ]?\d)")
_EP_RE = re.compile(r"(?i)(?:^|[.\-_ \[])(?:EP?)(\d{1,4})(?:\b|$)")
_EP_RANGE_RE = re.compile(r"(?i)\bE(\d{1,4})[.\-_~]?E(\d{1,4})\b")
_CN_EP_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百]+)\s*[集话期]")
_CN_SE_EP_RE = re.compile(
    r"第\s*([0-9一二三四五六七八九十百]+)\s*季[^\d]{0,6}第?\s*([0-9一二三四五六七八九十百]+)\s*[集话期]")
_CN_SE_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百]+)\s*季")
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}

# 规格词表（归一化 key -> 展示名），匹配用 \b 包裹
_RESOLUTIONS = {  # key 小写 -> 规范写法
    "2160p": "2160p", "4k": "2160p", "uhd": "2160p", "hd2160p": "2160p",
    "4ksdr": "2160p", "4khdr": "2160p",
    "1080p": "1080p", "hd1080p": "1080p",
    "720p": "720p", "480p": "480p",
}
_SOURCES = {  # key 小写 -> 规范写法（更特异的词在前：BluRay.Remux 取 Remux）
    "remux": "Remux", "web-dl": "WEB-DL", "webdl": "WEB-DL", "webrip": "WEBRip",
    "bluray": "BluRay", "blu-ray": "BluRay", "bdrip": "BDRip", "bd": "BDRip",
    "hdtv": "HDTV", "dvdrip": "DVDRip", "hd": "HDTV",
}
_CODECS = {
    "x264": "H.264", "h264": "H.264", "h.264": "H.264", "avc": "H.264",
    "x265": "H.265", "h265": "H.265", "h.265": "H.265", "hevc": "H.265",
    "xvid": "XviD", "av1": "AV1",
}
_AUDIOS = {
    "aac": "AAC", "flac": "FLAC", "ac3": "AC3", "ddp": "DD+", "dd": "DD",
    "truehd": "TrueHD", "atmos": "Atmos", "杜比全景声": "Atmos",
    "dts": "DTS", "opus": "Opus", "杜比数字": "DD",
}
_EXTRAS = {  # 附加标签（保留原词，仅大小写归一化常见写法）
    "hdr": "HDR", "dv": "DV", "dovi": "DV", "杜比视界": "DV",
    "10bit": "10bit", "8bit": "8bit", "60fps": "60fps", "120帧": "120fps",
}
_SUBS_RE = re.compile(
    r"(chs|cht|chteng|chs-eng|中文字幕|中英双字|中英字幕|双语字幕|中字|官方中字|简中|繁中|"
    r"简繁|简体内嵌|内嵌中英|中韩字幕|简繁内封|内封中字|特效字幕)", re.IGNORECASE)
_LANGS_RE = re.compile(r"(国粤双语|国语|普通话|粤语|英语原声|日语原声|韩语原声|English|Eng|KO_CN)")

# 发布组：尾部 -XXX / @XXX（排除规格词；词表兜底常见组名后缀特征）
_GROUP_TAIL_RE = re.compile(r"[-@]([A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff._@]*)$")
_GROUP_HINT_RE = re.compile(
    r"(组|Team|TV$|HD$|UHD$|web$|WiKi|CHD|DreamHD|BlackTV|PanWEB|AIU|HDS|"
    r"年糕|一番|404|人人|压|字幕)", re.IGNORECASE)
_SPEC_TOKEN_RE = re.compile(
    r"(?i)^(?:19|20)\d{2}b?|s\d{1,2}e?\d{0,4}|\d{1,4}x\d{1,3}|2160p?|1080p?|720p?|"
    "480p?|4k.*|uhd|hdr10?|dv|dovi|web-?dl|webrip|blu-?ray|bdrip|bd|hdtv|dvdrip|"
    "remux|x26[45]|h.?26[45]|hevc|avc|av1|xvid|aac|flac|ac-?3|ddp?|dd\\+|truehd|"
    "atmos|dts.*|opus|10bit|8bit|60fps|chs.*|eng| english|ep?\\d{1,4}$")


def _cn_to_int(s: str) -> int | None:
    s = s.strip()
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
    return total + num if (total or num) else None


@dataclass
class ParsedInfo:
    """文件名解析结果。season/episode/year 为 int 或 None；规格为归一化字符串。"""
    raw: str = ""
    ext: str = ""
    is_video: bool = False
    is_subtitle: bool = False
    junk: bool = False
    title: str = ""              # 主标题（优先中文段；纯英文名时即英文名）
    title_en: str = ""           # 次标题（中英双语名时的英文/拉丁段）
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    end_episode: int | None = None   # E01-E03 范围的尾集
    episode_title: str = ""      # 集标题（如 Chapter.One.The.Crawl）
    resolution: str | None = None    # 2160p / 1080p / 720p / 480p
    source: str | None = None        # WEB-DL / BluRay / Remux / HDTV / BDRip...
    codec: str | None = None         # H.264 / H.265 / AV1...
    audio: list[str] = field(default_factory=list)   # AAC / DD+5.1 / TrueHD.7.1 / Atmos
    subtitle: list[str] = field(default_factory=list)  # 中英双字 / CHS-ENG / 官方中字
    languages: list[str] = field(default_factory=list)  # 国语 / 粤语 / English
    extras: list[str] = field(default_factory=list)  # HDR / DV / 10bit / 60fps
    group: str | None = None         # 发布组/压制组
    titleless: bool = False          # 文件名无标题（仅季集/编号，无法定位）


class MessyNameParser:
    """从乱七八糟的影视资源文件名中提取结构化信息并生成规范文件名。"""

    # ---- 主入口 ----

    def parse(self, filename: str) -> ParsedInfo:
        info = ParsedInfo(raw=filename)
        stem = filename
        if "." in stem and os.path.splitext(stem)[1].lower() in (VIDEO_EXTS | SUB_EXTS):
            info.ext = os.path.splitext(stem)[1].lower()
            stem = stem[: -len(info.ext)]
        info.is_video = info.ext in VIDEO_EXTS
        info.is_subtitle = info.ext in SUB_EXTS
        if JUNK_RE.search(stem):
            info.junk = True

        # 方括号内容：规格/发布组常藏在这里（[4K原盘][内封中字] / [TSKS][2022]）。
        # 保留括号原样，便于后续用 _BRACKETS_RE 二次提取（组名/标题兜底）
        bracket_text = " ".join(m.group(0) for m in _BRACKETS_RE.finditer(stem))
        stem_wo_brackets = _BRACKETS_RE.sub(" ", stem)

        # 季集（在去括号文本上匹配，位置用于切标题）
        self._extract_se(info, stem_wo_brackets)
        # 年份（排除火影2016p 这类假年份；括号内容也参与，如 月光光心慌慌(2018)）
        ym = _YEAR_RE.search(stem_wo_brackets) or _YEAR_RE.search(bracket_text)
        if ym:
            info.year = int(ym.group(1))
        # 规格（词表匹配：去括号文本 + 括号内容都参与）
        self._extract_specs(info, stem_wo_brackets + " " + bracket_text)
        # 发布组（尾部 -XXX / @XXX / 括号内容）
        self._extract_group(info, stem_wo_brackets, bracket_text)
        # 标题：首个"结构 token"（季集/年份/规格）之前的部分
        self._extract_titles(info, stem_wo_brackets, bracket_text)

        # 纯编号/无标题判定：标题区剥掉季集/编号 token 后无剩余
        if info.is_video and not _has_words(info.title) \
                and not _has_words(info.title_en):
            core = [t for t in _TOKEN_SPLIT.split(stem_wo_brackets)
                    if t and not self._is_meta(t)]
            info.title, info.title_en, info.titleless = "", "", not core
        return info

    # ---- 规范文件名 ----

    def canonical(self, name: str | ParsedInfo, *, title: str | None = None,
                  title_en: str | None = None,
                  season: int | None = None, episode: int | None = None,
                  year: int | None = None, style: str = "dot",
                  include_group: bool = True, include_audio: bool = True,
                  include_extras: bool = True) -> str:
        """生成规范文件名。显式参数（任务上下文已知的标题/季集）优先于解析值。

        title/title_en 同时传时按 en.title 格式（英文 dot 连接 + 中文）；
        仅一个传时直接用，否则回退到 info.title / info.title_en。"""
        info = name if isinstance(name, ParsedInfo) else self.parse(name)
        # 选标题段：优先显式参数
        cn = title or info.title or ""
        en = title_en or info.title_en or ""
        if en and cn:
            # 中英文都有 → en.title（英文 dot 连接，中文原文）
            en_dotted = re.sub(r"\s+", ".", en.strip())
            t = f"{en_dotted}.{cn.strip()}"
        elif en:
            t = re.sub(r"\s+", ".", en.strip())
        elif cn:
            t = cn.strip()
        else:
            t = ""
        # title 段里只要出现拉丁字母，空格统一 dot 化（如 Cyberpunk Edgerunners 被塞进 info.title 时）
        if t and re.search(r"[A-Za-z]", t) and style == "dot":
            t = re.sub(r"\s+", ".", t)
        if not t:  # 无标题：保留原文件名
            return info.raw
        parts = [t]
        s = season if season is not None else info.season
        e = episode if episode is not None else info.episode
        y = year or info.year
        if e is not None:
            se = f"S{(s or 1):02d}E{e:02d}"
            if info.end_episode and episode is None:
                se += f"-E{info.end_episode:02d}"
            parts.append(se)
            if y:
                parts.append(str(y))
        elif s is not None:
            parts.append(f"S{s:02d}")
        elif y:
            parts.append(f"({y})" if style == "space" else str(y))
        for v in (info.resolution, info.source, info.codec):
            if v:
                parts.append(v)
        if include_audio and info.audio:
            a = [x for x in info.audio if x != "Atmos"]
            if "Atmos" in info.audio:
                a.append("Atmos")
            parts.append(".".join(a))
        if include_extras and info.extras:
            parts.append(".".join(dict.fromkeys(info.extras)))
        if include_group and info.group:
            parts[-1] += "-" + info.group
        ext = info.ext if (info.ext in VIDEO_EXTS | SUB_EXTS) else ""
        body = (" " if style == "space" else ".").join(parts)
        return f"{body}{ext}" if ext else body

    # ---- 季集提取 ----

    def _extract_se(self, info: ParsedInfo, stem: str) -> None:
        if m := _SEEP_RE.search(stem):  # S01E01 / S01.E01 / S01E01-E03
            info.season, info.episode = int(m.group(1)), int(m.group(2))
            if m.group(3):
                info.end_episode = int(m.group(3))
            self._episode_title_from(info, stem, m.end())
            return
        if m := _SE_DOT_EP_RE.search(stem):  # SE01.01
            info.season, info.episode = int(m.group(1)), int(m.group(2))
            return
        # 整个文件名就是裸数字（06.mkv / 1001.mp4）：唯一信息是集数
        if re.fullmatch(r"\d{1,4}", stem.strip()):
            info.episode = int(stem.strip())
            return
        if m := _EP_RANGE_RE.search(stem):  # E01-E03（无季）
            info.episode, info.end_episode = int(m.group(1)), int(m.group(2))
            return
        if m := _CN_SE_EP_RE.search(stem):  # 第五季第一集
            info.season = _cn_to_int(m.group(1))
            info.episode = _cn_to_int(m.group(2))
            return
        if m := _NXN_RE.search(stem):  # 1x01
            info.season, info.episode = int(m.group(1)), int(m.group(2))
            return
        if m := _CN_EP_RE.search(stem):  # 第X集
            info.episode = _cn_to_int(m.group(1))
            return
        if m := _EP_RE.search(stem):  # E01 / EP001
            info.episode = int(m.group(1))
            return
        if m := _CN_SE_RE.search(stem):  # 第X季（整季包）
            info.season = _cn_to_int(m.group(1))
            return
        if m := _S_ONLY_RE.search(stem):  # S02（目录/整季）
            info.season = int(m.group(1))

    def _episode_title_from(self, info: ParsedInfo, stem: str, pos: int) -> None:
        """SxxEyy 之后的集标题（Chapter.One.The.Crawl），到年份/规格 token 为止。"""
        seg = stem[pos:].strip(" .-_")
        toks = []
        for t in _TOKEN_SPLIT.split(seg):
            if not t:
                continue
            if self._is_spec_word(t) or _YEAR_RE.fullmatch(t):
                break
            toks.append(t)
        info.episode_title = " ".join(toks)

    # ---- 规格提取 ----

    def _extract_specs(self, info: ParsedInfo, text: str) -> None:
        lo = text.lower()
        xlo = lo.replace("×", ".x")  # 全角乘号（HD2160P×265）补分隔便于匹配 x265
        for key, val in _RESOLUTIONS.items():
            if re.search(rf"(?<![\da-z]){re.escape(key)}(?![\da-z])", lo):
                info.resolution = val
                break
        for key, val in _SOURCES.items():
            if re.search(rf"(?<![\da-z]){re.escape(key)}(?![\da-z])", lo):
                info.source = val
                break
        for key, val in _CODECS.items():
            if re.search(rf"(?<![\da-z]){re.escape(key)}(?![\da-z])", xlo):
                info.codec = val
                break
        audio: list[tuple[int, str]] = []
        for key, val in _AUDIOS.items():
            if (m := re.search(rf"(?<![\da-z]){re.escape(key)}(?![\da-z])", lo)) \
                    and val not in [v for _, v in audio]:
                audio.append((m.start(), val))
        audio.sort()  # 按文件名中出现顺序
        # 声道规格连写在最后一个非 Atmos 音频词后（DD+5.1 / TrueHD7.1 / AAC5.1）；
        # 纯 Atmos 或无音频词时不合并（Atmos.7.1 / DTS-X.7.1 已由 DTS 词命中）
        ch = re.search(r"(?<!\d)([57])\.([01])(?!\d)", lo)
        if audio and ch:
            non_atmos = [i for i, (_, v) in enumerate(audio) if v != "Atmos"]
            if non_atmos:
                pos, v = audio[non_atmos[-1]]
                audio[non_atmos[-1]] = (pos, v + f"{ch.group(1)}.{ch.group(2)}")
        info.audio = [v for _, v in audio]
        extras: list[tuple[int, str]] = []  # 按出现位置排序（DV.HDR ≠ HDR.DV）
        for key, val in _EXTRAS.items():
            if key.isdigit() or "帧" in key:
                if (m := re.search(re.escape(key), text)):
                    extras.append((m.start(), val))
            elif (m := re.search(rf"(?<![\da-z]){re.escape(key)}(?![\da-z])", lo)):
                extras.append((m.start(), val))
        info.extras = [v for _, v in sorted(extras)]
        info.extras = list(dict.fromkeys(info.extras))
        info.subtitle = list(dict.fromkeys(
            m.group(1) for m in _SUBS_RE.finditer(text)))
        info.languages = list(dict.fromkeys(
            m.group(1) for m in _LANGS_RE.finditer(text)))

    # ---- 发布组 ----

    def _extract_group(self, info: ParsedInfo, stem: str, bracket: str) -> None:
        # 括号内组名（[TSKS] / [WIKNB]）优先
        for b in _BRACKETS_RE.findall(bracket):
            if re.fullmatch(r"(?i)[A-Z0-9]{2,20}", b.strip()) and \
                    not b.strip().isdigit():
                info.group = b.strip().upper()
                return
        tail = stem.rstrip(" .-_")
        m = _GROUP_TAIL_RE.search(tail)
        if m:
            cand = m.group(1).strip("._")
            # 候选不能是规格/年份/季集 token，且带组名特征；
            # 剥掉混入的字幕/语言标签前缀（CHS-ENG.星球4K压制组 → 星球4K压制组）
            cand = re.sub(r"^(?:CHS?|CHT|ENG|KO_?CN|简|繁)[.\-_]", "", cand,
                          flags=re.IGNORECASE)
            # 候选不能是规格/年份/季集 token，且带组名特征
            if cand and not self._is_spec_word(cand) and not self._is_meta(cand) \
                    and _GROUP_HINT_RE.search(cand) and len(cand) <= 20:
                info.group = cand

    # ---- 标题 ----

    def _extract_titles(self, info: ParsedInfo, stem: str,
                        bracket_text: str = "") -> None:
        tokens = [t for t in _TOKEN_SPLIT.split(stem) if t]
        head: list[str] = []
        year_seen = 0
        for t in tokens:
            # 标题 token 内嵌干扰：尾缀圆括号年份（月光光心慌慌(2018)）、
            # 假年份编号（火影2016p —— 2016p 是压制编号不是年份）
            t = re.sub(r"[（(](?:19|20)\d{2}[)）]$", "", t)
            t = re.sub(r"(?i)(?:19|20)\d{2}p$", "", t)
            if not t:
                continue
            if _YEAR_RE.fullmatch(t):
                year_seen += 1
                rest = tokens[tokens.index(t) + 1:]
                # 《2012》特例：首个 token 是年份且后面还有年份 → 当片名，年份取后面那个
                if not head and year_seen == 1 and \
                        any(_YEAR_RE.fullmatch(x) for x in rest):
                    head.append(t)
                    info.year = int(next(x for x in rest
                                         if _YEAR_RE.fullmatch(x)))
                    continue
                break
            if self._is_spec_word(t):
                break
            if self._is_meta(t):
                # 前缀编号（Top013 / Z99546）跳过不截断；季集标注截断
                if not head and re.fullmatch(r"(?i)[a-z]{1,3}\d{3,6}", t):
                    continue
                if not head and t.isdigit():
                    continue
                break
            if info.group and t == info.group:
                break
            # 标题尾部混入的发布组/压制组（星际穿越-霸王龙压制组T-Rex）
            if head and re.search(r"压制组|字幕组|小组|年糕|一番|404|字幕$", t):
                break
            # 标题连写集数（狂飙01 → 狂飙 + E01）；共助2 单数字、Apollo13 不拆
            if info.episode is None:
                m2 = re.fullmatch(r"(.+[^0-9])(0\d{1,3}|\d{2,3})", t)
                if m2 and (m2.group(2)[0] == "0" or _CJK_RE.search(m2.group(1))) \
                        and not _YEAR_RE.fullmatch(m2.group(2)):
                    info.episode = int(m2.group(2))
                    head.append(m2.group(1))
                    break
            head.append(t)
        if not head:
            # 方括号标题兜底（[TSKS][2022][Alienoid][(1080P)]：主名在括号里）
            for b in _BRACKETS_RE.findall(bracket_text):
                bt = b.strip()
                if _YEAR_RE.fullmatch(bt) or bt.isdigit() \
                        or self._is_spec_word(bt) or (info.group and bt == info.group):
                    continue
                if bt and _has_words(bt) and len(bt) >= 2:
                    head = [bt]
                    break
            if not head:
                return
        text = " ".join(head)
        # 尾部组名剥离（星际穿越-霸王龙压制组T-Rex → 星际穿越）
        text = re.sub(
            r"\s*[-@]\s*\S{0,30}?(?:压制组|字幕组|小组|字幕|组|Team|T-Rex|"
            r"WiKi|CHD|DreamHD|BlackTV|PanWEB|HDS|AIU|404|年糕|一番)\S{0,10}$",
            "", text).strip(" .-_—-")
        if not text:
            return
        head = text.split(" ")
        # 中英双标题拆分：连续 CJK 段为主标题，连续拉丁段为英文名
        cjk_runs = []
        cur_c, cur_l = [], []
        for tok in text.split(" "):
            if _CJK_RE.search(tok):
                cur_c.append(tok)
            else:
                if cur_c:
                    cjk_runs.append(" ".join(cur_c))
                    cur_c = []
                cur_l.append(tok)
        if cur_c:
            cjk_runs.append(" ".join(cur_c))
        en = " ".join(t for t in cur_l if t)
        cn = " ".join(r for r in cjk_runs if r)
        # 清理分隔残渣（~、连续点、全角括号内容保留中文）
        clean = lambda s: re.sub(r"[~*]+", "", s).strip(" .-_—-")
        info.title = clean(cn)
        info.title_en = clean(en)
        if not info.title and info.title_en:
            info.title, info.title_en = info.title_en, ""

    # ---- 工具 ----

    def _is_meta(self, t: str) -> bool:
        """季集/集数/裸编号 token（标题区判定时剥除）。"""
        tt = t.lower().strip("()[]【】")
        if not tt:
            return True
        if re.fullmatch(r"(?i)s\d{1,2}e?\d{0,4}", tt) or \
                re.fullmatch(r"(?i)se\d{1,2}([.\-_ ]?\d{1,4})?", tt):
            return True
        if re.fullmatch(r"(?i)(ep?)?\d{1,4}", tt) and not _YEAR_RE.fullmatch(tt):
            # 4 位纯数字在年份区间内视为可能的片名（《2012》），否则视为集数
            if not (tt.isdigit() and len(tt) == 4 and 1900 <= int(tt) <= 2099):
                return True
        if re.fullmatch(r"(?i)[a-z]{1,3}\d{3,6}", tt):
            # 字母前缀+长数字视为编号（Z99546 / Top013 的编号段；THX1138 类
            # 片名极罕见，牺牲之）
            return True
        if re.fullmatch(r"第\s*[0-9一二三四五六七八九十百]+\s*[季集话期]?", tt):
            return True
        # 第五季第三集（中文季+集连写一个 token）
        if re.fullmatch(
                r"第\s*[0-9一二三四五六七八九十百]+\s*季\s*第?\s*"
                r"[0-9一二三四五六七八九十百]+\s*[集话期]", tt):
            return True
        if re.fullmatch(r"(?i)\d{1,2}x\d{1,3}", tt):
            return True
        return False

    def _is_spec_word(self, t: str) -> bool:
        lo = t.lower()
        if _YEAR_RE.fullmatch(t):
            return False
        for table in (_RESOLUTIONS, _SOURCES, _CODECS, _AUDIOS, _EXTRAS):
            if lo in table:
                return True
        if _SPEC_TOKEN_RE.fullmatch(t):
            return True
        return bool(re.fullmatch(
            r"(?i)(hdr10?|dolby|vision|atmos|dts-x|dts-hd|truehd|ddp?|10bit|"
            r"60fps|web|remux|nf|max|amzn|chs|cht|eng|简繁|中字|官方中字|"
            r"中英双字|中英字幕|内嵌|国语|粤语|国粤双语|English)", t))


def _has_words(s: str) -> bool:
    """是否包含实义字符（字母/数字/汉字）。"""
    return bool(s) and any(c.isalnum() or _CJK_RE.match(c) for c in s)


def spec_dict(info_or_name: "str | ParsedInfo") -> dict:
    """展示用规格字典（resolution/source/codec/audio/subtitle/languages/extras）。

    取代旧 matcher.parse_file_spec：由 MessyNameParser 统一解析，
    词表更全（4K/HD2160P×265、HDR/DV/10bit、杜比全景声等），缺省维度省略。"""
    info = info_or_name if isinstance(info_or_name, ParsedInfo) \
        else parser.parse(info_or_name)
    spec: dict = {}
    for key in ("resolution", "source", "codec"):
        val = getattr(info, key)
        if val:
            spec[key] = normalize_resolution(val) if key == "resolution" else val
    for key in ("audio", "subtitle", "languages", "extras"):
        if getattr(info, key):
            spec[key] = list(getattr(info, key))
    return spec


_RESOLUTION_NORMALIZE = {
    # 4K 等价归一
    "4K": "2160p", "UHD": "2160p", "UHD4K": "2160p",
    "4320P": "2160p", "2160P": "2160p", "2160": "2160p",
    # 8K
    "8K": "4320p", "4320P": "4320p",
    # 1080p 相关
    "FHD": "1080p", "1080P": "1080p",
    # 720p
    "HD": "720p", "720P": "720p",
}


def normalize_resolution(val: str | None) -> str | None:
    """把各种分辨率写法归一化成唯一形式。

    4K/UHD/2160p → "2160p"；8K → "4320p"；FHD/1080p → "1080p"；HD/720p → "720p"。
    其他格式原样返回（如 "480p"、"576p"）。"""
    if not val:
        return None
    key = str(val).strip().upper().replace("HD2160P", "2160P")
    return _RESOLUTION_NORMALIZE.get(key, str(val).strip().lower())


# 模块级单例，调用方直接 import 使用
parser = MessyNameParser()


def canonical_with_context(name: str, *, search_title: str = "",
                           share_title: str = "", folder: str = "",
                           **kwargs) -> str:
    """结合上下文生成规范文件名（转存/展示场景用）。

    文件名本身能解析出标题时忽略上下文；纯季集/编号等无标题名
    （01.mkv / SE03.11.mp4）按 搜索名 → 分享名 → 文件夹名 顺序取
    第一个可解析出标题的上下文补全。上下文名本身也可能是乱七八糟
    命名（"三体 2023 4K全集"），先经解析器提纯再使用。

    kwargs 透传 canonical()（style/include_group/season/episode 等）。"""
    info = parser.parse(name)
    if _has_words(info.title) or _has_words(info.title_en):
        return parser.canonical(info, **kwargs)
    for ctx in (search_title, share_title, folder):
        ctx = (ctx or "").strip()
        if not ctx:
            continue
        t = parser.parse(ctx).title
        if _has_words(t):
            return parser.canonical(info, title=t, **kwargs)
    return parser.canonical(info, **kwargs)
