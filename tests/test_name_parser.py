"""乱七八糟文件名解析器测试：用 tests/data/messy_filenames.txt 的真实样本。

断言策略：每个代表性模式断言关键字段（标题/季集/年份/分辨率等），
不追求全字段一致；canonical 断言规范输出格式。"""
import os

import pytest

from app.name_parser import MessyNameParser, VIDEO_EXTS

P = MessyNameParser()
SAMPLES = os.path.join(os.path.dirname(__file__), "data", "messy_filenames.txt")


def load_sample_names() -> list[str]:
    with open(SAMPLES, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh
                if ln.strip() and not ln.startswith("#")]


# ---- 代表性模式单测 ----

def test_standard_scene_name():
    i = P.parse("The.Wandering.Earth.2019.2160p.WEB-DL.x265.10bit.HDR.DDP.5.1.Atmos.mkv")
    assert i.title == "The.Wandering.Earth" or i.title == "The Wandering Earth"
    assert i.title.replace(".", " ") == "The Wandering Earth"
    assert i.year == 2019 and i.resolution == "2160p" and i.source == "WEB-DL"
    assert i.codec == "H.265" and "10bit" in i.extras and "HDR" in i.extras
    assert "DD+5.1" in i.audio and "Atmos" in i.audio
    assert i.season is None and i.episode is None and i.is_video


def test_movie_chinese_dot_name():
    i = P.parse("让子弹飞.2010.1080p.BluRay.Remux.AVC.DTS-HD.MA.5.1-Wu Ke.mkv")
    assert i.title == "让子弹飞" and i.year == 2010
    assert i.resolution == "1080p" and i.source == "Remux" and i.codec == "H.264"


def test_series_s01e01_with_episode_title():
    i = P.parse("Stranger.Things.S05E01.Chapter.One.The.Crawl.2160p.NF.WEB-DL.DDP.5.1.Atmos.H.265.mkv")
    assert i.title.replace(".", " ") == "Stranger Things"
    assert i.season == 5 and i.episode == 1
    assert i.episode_title == "Chapter One The Crawl"
    assert i.resolution == "2160p" and i.source == "WEB-DL" and i.codec == "H.265"


def test_s01_dot_e01_variant():
    i = P.parse("S01.E07.2023.2160p.WEB-DL.H265.60fps.AAC-PanWEB.mp4")
    assert i.season == 1 and i.episode == 7 and i.year == 2023
    assert i.resolution == "2160p" and i.group == "PanWEB"


def test_se01_dot_01_variant():
    i = P.parse("老友记.H265.1080P.SE01.01.mkv")
    assert i.title == "老友记" and i.season == 1 and i.episode == 1
    assert i.codec == "H.265" and i.resolution == "1080p"


def test_e_only_variant():
    i = P.parse("后宫·甄嬛传.Empresses.in.the.Palace.2011.E12.WEB-DL.4k.H265.10bit.AAC-AIU.mp4")
    assert i.title == "后宫·甄嬛传"
    assert i.title_en.replace(".", " ") == "Empresses in the Palace"
    assert i.episode == 12 and i.year == 2011 and i.resolution == "2160p"
    assert i.group == "AIU"


def test_ep_three_digit():
    i = P.parse("火影2016p.EP014.mp4")
    assert i.title == "火影" and i.episode == 14
    assert i.year is None  # 2016p 是假年份（2016p 像分辨率，实为编号）
    assert i.resolution is None


def test_ep_only_prefix():
    i = P.parse("Some.Show.EP07.mkv")
    assert i.episode == 7


def test_cn_season_episode():
    i = P.parse("怪奇物语.第五季第三集.4K内嵌中英字幕版.Stranger.Things.S05E03.Chapter.One.The.Crawl.HD2160P×265.AAC.English.CHS-ENG.星球4K压制组.mp4")
    assert i.title == "怪奇物语"
    assert i.season == 5 and i.episode == 3
    assert i.resolution == "2160p" and i.codec == "H.265"
    assert any("中英" in s for s in i.subtitle)
    assert i.group and "星球" in i.group


def test_cn_episode_only():
    i = P.parse("狂飙01.mp4")
    assert i.title == "狂飙" and i.episode == 1


def test_space_style_with_cn_episode_marker():
    i = P.parse("Day and Night S01E01 - 第 1 集 - 2160p WEB-DL HDR H265 10bit AAC.mkv")
    assert i.title == "Day and Night"
    assert i.season == 1 and i.episode == 1 and i.resolution == "2160p"
    assert i.source == "WEB-DL" and "HDR" in i.extras and i.codec == "H.265"


def test_fullwidth_brackets():
    i = P.parse("月光光心慌慌(2018)[4K蓝光原盘REMUX典藏版][中文字幕+特效字幕]")
    assert i.title == "月光光心慌慌" and i.year == 2018
    assert i.resolution == "2160p"
    assert any("中" in s for s in i.subtitle)


def test_bracket_group():
    i = P.parse("[TSKS][2022][Alienoid][(1080P)][KO_CN].mkv")
    assert i.year == 2022 and i.title == "Alienoid"
    assert i.resolution == "1080p"


def test_remux_full_case():
    i = P.parse("Zootopia.2016.UHD.BluRay.2160p.Atmos.TrueHD.7.1.HDR.x265.10bit-CHD.mkv")
    assert i.title == "Zootopia"
    assert i.year == 2016 and i.resolution == "2160p" and i.source == "BluRay"
    assert i.codec == "H.265" and "Atmos" in i.audio and "TrueHD7.1" in i.audio
    assert "HDR" in i.extras and i.group == "CHD"


def test_1x01_variant():
    i = P.parse("Show.Name.1x03.720p.HDTV.x264.mp4")
    assert i.season == 1 and i.episode == 3 and i.resolution == "720p"


def test_pure_number_ep():
    assert P.parse("06.mkv").episode == 6
    assert P.parse("1001.mp4").episode == 1001
    assert P.parse("001.mkv").episode == 1


def test_movie_2012_not_episode():
    i = P.parse("2012.2009.1080p.BluRay.x264.mkv")  # 片名《2012》
    assert i.title == "2012" and i.year == 2009


def test_subtitle_and_non_video():
    i = P.parse("钢铁侠 2160p remux (2008).chs.ass")
    assert i.is_subtitle and not i.is_video
    assert i.title == "钢铁侠" and i.year == 2008
    assert P.parse("season.nfo").is_video is False
    assert P.parse("简中.srt").is_subtitle


def test_junk_name():
    assert P.parse("River.S01E01.Trailer.1080p.mp4").junk


def test_numbered_prefix_title():
    i = P.parse("Top013.星际穿越-霸王龙压制组T-Rex.mp4")
    assert "星际穿越" in i.title


def test_dash_in_title_untouched():
    i = P.parse("绝-命-D师 (2008)")
    assert i.year == 2008  # 标题原样保留（敏感词改写无法自动还原）


def test_no_ext_entry():
    i = P.parse("Cyberpunk.Edgerunners.S02")
    assert i.season == 2 and i.ext == ""


# ---- canonical 规范化 ----

def test_canonical_movie():
    s = P.canonical("Zootopia.2016.UHD.BluRay.2160p.Atmos.TrueHD.7.1.HDR.x265.10bit-CHD.mkv")
    assert s == "Zootopia.2016.2160p.BluRay.H.265.TrueHD7.1.Atmos.HDR.10bit-CHD.mkv"


def test_canonical_series():
    s = P.canonical("老友记.H265.1080P.SE01.01.mkv")
    assert s == "老友记.S01E01.1080p.H.265.mkv"


def test_canonical_with_context_override():
    # 任务上下文已知标题与季集（文件名 SE01.02 无标题）
    s = P.canonical("SE01.02.mkv", title="绝命毒师", season=1, episode=2)
    assert s == "绝命毒师.S01E02.mkv"


def test_canonical_keep_raw_when_titleless():
    assert P.canonical("Z99546-2024-2160p.mkv") == "Z99546-2024-2160p.mkv"


def test_canonical_space_style():
    s = P.canonical("让子弹飞.2010.1080p.BluRay.Remux.AVC.DTS-HD.MA.5.1-Wu Ke.mkv",
                    style="space")
    assert s.startswith("让子弹飞 (2010) 1080p")


def test_canonical_whole_season():
    s = P.canonical("Cyberpunk.Edgerunners.S02")
    assert s == "Cyberpunk.Edgerunners.S02"


# ---- 真实样本全量冒烟：解析不抛异常、视频文件不产生崩溃字段 ----

def test_all_samples_parse_without_error():
    names = load_sample_names()
    assert len(names) > 900
    for n in names:
        info = P.parse(n)
        assert info.raw == n
        if info.is_video:
            assert info.ext in VIDEO_EXTS


def test_all_samples_season_episode_sanity():
    for n in load_sample_names():
        info = P.parse(n)
        # SE01.01 类文件名必须出季集
        if n.lower().startswith("se") and "." in n[:6]:
            assert info.season and info.episode, n
        # SxxEyy 必须识别
        m = __import__("re").search(r"(?i)S(\d{2})E(\d{2})", n)
        if m and info.is_video:
            assert (info.season, info.episode) == (int(m.group(1)), int(m.group(2))), n


@pytest.mark.parametrize("name,expected", [
    ("Zootopia.2016.UHD.BluRay.2160p.Atmos.TrueHD.7.1.HDR.x265.10bit-CHD.mkv",
     "Zootopia.2016.2160p.BluRay.H.265.TrueHD7.1.Atmos.HDR.10bit-CHD.mkv"),
    ("Stranger.Things.S05E05.2025.2160p.NF.WEB-DL.H265.DV.HDR.DDP.5.1.Atmos.mkv",
     "Stranger.Things.S05E05.2025.2160p.WEB-DL.H.265.DD+5.1.Atmos.DV.HDR.mkv"),
])
def test_canonical_doc_examples(name, expected):
    assert P.canonical(name) == expected
