"""MovieMatcher 单元测试（需求第 12 条验收项全覆盖）。"""
from app.tmdb import VideoMeta
from app.matcher import MovieMatcher, parse
from app.providers.base import CloudFile

M = MovieMatcher(threshold=0.6)


def cf(name: str, fid: str = "f1") -> CloudFile:
    return CloudFile(fid=fid, name=name, size=1024, is_dir=False)


# ---------- parse ----------

def test_parse_sxxexx_with_year():
    p = parse("余罪.S01E12.2016.1080P.WEB-DL.H264.AAC.mp4")
    assert p.title == "余罪"
    assert p.season == 1 and p.episode == 12
    assert p.year == 2016
    assert p.resolution == "1080p"
    assert p.ext == ".mp4"


def test_parse_movie_dot_separated():
    p = parse("The.Avengers.2012.1080p.BluRay.x264.mkv")
    assert p.title == "The Avengers"
    assert p.year == 2012
    assert p.episode is None
    assert p.resolution == "1080p"


def test_parse_chinese_episode_number():
    p = parse("余罪 第12集 1080p.mp4")
    assert p.title == "余罪"
    assert p.episode == 12


def test_parse_chinese_number_word():
    p = parse("庆余年.第一季.第03集.2024.4K.mp4")
    assert p.title == "庆余年"
    assert p.season == 1 and p.episode == 3
    assert p.year == 2024
    assert p.resolution == "4k"


def test_parse_title_starting_with_year():
    p = parse("2012.2009.1080p.BluRay.mkv")  # 片名《2012》，2009 年出品
    assert p.title == "2012"
    assert p.year == 2009


def test_parse_bracket_group_prefix():
    p = parse("[Group]Some.Movie.2021.720p.WEBRip.mkv")
    assert p.title == "Some Movie"
    assert p.year == 2021


# ---------- 匹配 ----------

def _movie_meta(title="The Avengers", year=2012):
    m = VideoMeta({"imdb_id": "tt0848228", "name": title, "type": "movie",
                   "releaseInfo": str(year)})
    return m


def _series_meta(title="余罪", year=2016):
    return VideoMeta({"imdb_id": "tt5636724", "name": title, "type": "series",
                      "releaseInfo": str(year),
                      "videos": [{"season": 1, "episode": 12, "name": "一切归零"}]})


def test_similar_title_matched():
    res = M.match([cf("The.Avengers.2012.1080p.BluRay.x264.mkv")], _movie_meta())
    assert res and res[0].score >= 0.6


def test_similar_but_wrong_year_rejected():
    """不同年份不得错误匹配（硬否决）。"""
    files = [cf("The.Avengers.2019.1080p.BluRay.x264.mkv")]
    assert M.match(files, _movie_meta(year=2012)) == []


def test_junk_files_excluded():
    files = [cf("The.Avengers.2012.1080p.TRAILER.mp4"),
             cf("sample-The.Avengers.2012.mkv"),
             cf("The.Avengers.2012.花絮.mkv")]
    assert M.match(files, _movie_meta()) == []


def test_subtitle_and_non_video_excluded():
    files = [cf("The.Avengers.2012.1080p.chs.srt"),
             cf("The.Avengers.2012.1080p.jpg")]
    assert M.match(files, _movie_meta()) == []


def test_series_episode_hard_match():
    """剧集必须集数正确：E12 命中，E11 不命中。"""
    files = [cf("余罪.S01E11.2016.1080p.mp4", "f11"),
             cf("余罪.S01E12.2016.1080p.mp4", "f12")]
    res = M.match(files, _series_meta(), season=1, episode=12)
    assert [r.file.fid for r in res] == ["f12"]


def test_series_episode_required():
    """文件无集数信息时不参与剧集匹配。"""
    files = [cf("余罪.2016.1080p.mp4")]
    assert M.match(files, _series_meta(), season=1, episode=12) == []


def test_series_chinese_episode_matched():
    files = [cf("余罪 第12集 1080p.mp4")]
    res = M.match(files, _series_meta(), season=1, episode=12)
    assert res and res[0].score >= 0.6


def test_cross_language_pinyin_match():
    """跨语言匹配：TMDB 英文名 ↔ 网盘中文名（拼音桥接）。"""
    meta_en = VideoMeta({"imdb_id": "tt5925192", "name": "Yu Zui", "type": "series",
                         "releaseInfo": "2016"})
    files = [cf("余罪.S01E12.2016.1080P.WEB-DL.H264.AAC.mp4")]
    res = M.match(files, meta_en, season=1, episode=12)
    assert res and res[0].score >= 0.6


def test_low_confidence_no_result():
    """标题完全不相似时无候选。"""
    files = [cf("Random.Show.2012.1080p.mkv")]
    assert M.match(files, _movie_meta()) == []


def test_best_returns_none_on_tie():
    """同分歧义时不返回（宁缺毋滥）。"""
    files = [cf("The.Avengers.2012.1080p.mkv", "a"),
             cf("The.Avengers.2012.720p.mkv", "b")]
    assert M.best(files, _movie_meta()) is None


def test_best_below_threshold_none():
    files = [cf("The.Avenge.rs.2012.1080p.mkv")]  # 模糊但不够相似
    assert M.best(files, _movie_meta(year=2010)) is None


# ---------- 阶段 5：裸集数提取 / 文件规格解析 ----------

def test_episode_of_bare_and_normal_names():
    from app.matcher import episode_of
    assert episode_of("06.mkv") == 6
    assert episode_of("EP06.mp4") == 6
    assert episode_of("e12.MP4") == 12
    assert episode_of("余罪.S01E12.2016.1080p.mp4") == 12  # parse 命中优先
    assert episode_of("余罪 第12集 1080p.mp4") == 12
    assert episode_of("余罪.2016.1080p.mp4") is None  # 无集数信息
    assert episode_of("封面.jpg") is None


def test_parse_file_spec_full():
    from app.matcher import parse_file_spec
    spec = parse_file_spec("余罪.S01E12.2016.2160p.WEB-DL.H265.DDP5.1.chs.mp4")
    assert spec["resolution"] == "4K"
    assert spec["source"] == "WEB-DL"
    assert spec["codec"] == "H.265"
    assert spec["audio"] == ["DD+", "5.1"]
    assert spec["subtitle"] == ["简体"]


def test_parse_file_spec_minimal_and_dedup():
    from app.matcher import parse_file_spec
    assert parse_file_spec("movie.mkv") == {}  # 无任何标注
    spec = parse_file_spec("Show.S01.1080p.AAC.AAC.mp4")
    assert spec["resolution"] == "1080p"
    assert spec["audio"] == ["AAC"]  # 重复标注去重


def test_parse_file_spec_subtitle_variants():
    from app.matcher import parse_file_spec
    spec = parse_file_spec("庆余年.第二季.2024.2160p.简繁.国粤双语.mkv")
    assert spec["resolution"] == "4K"
    assert spec["subtitle"] == ["简繁"]
    assert spec["languages"] == ["国粤双语"]
