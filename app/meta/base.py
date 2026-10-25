"""元数据提供者基类：定义 fetch_meta / fetch_meta_by_keyword 两个抽象接口。

VideoMeta 数据类也放在这里——它是所有元数据源统一的输出格式，
各 provider 派生类把各自数据源的原始返回转换成 VideoMeta 实例。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class VideoMeta:
    """影片元数据统一格式：所有 provider 派生类都输出这个结构。

    title 为主显示名（中文优先），name_zh / name_en 为双语；
    poster_path / backdrop_path 是 TMDB 风格的路径片段（如 /abc.jpg），
    完整 URL 由 poster_url() / backdrop_url() 拼接——非 TMDB provider
    可以自定义 image_base 覆盖拼接前缀。

    标识符字段（解决多数据源 ID 共存问题）：
      - source_id:    这次查询的原始 ID（可能是 imdb_id / tmdb_id / douban_id
                      / 关键词字符串）
      - source_kind:  source_id 属于哪种（"imdb" / "tmdb" / "douban" / "keyword"）
      - canonical_id: 统一主键（当前就是 IMDb ID；没有 IMDb ID 时退化为 source_id）"""

    # 默认图床（TMDB image CDN）；派生类可覆盖
    IMAGE_BASE = "https://image.tmdb.org/t/p"

    def __init__(self, meta: dict, *, image_base: str | None = None,
                 source_id: str = "", source_kind: str = ""):
        # 原始查询标识（派生类填，调用方用）
        self.source_id: str = source_id or meta.get("source_id", "") or meta.get("imdb_id", "")
        self.source_kind: str = source_kind or meta.get("source_kind", "") or "imdb"
        # 统一主键（能查到 IMDb ID 就用它，否则退化为 source_id）
        self.canonical_id: str = (meta.get("imdb_id")
                                  or meta.get("id", "")
                                  or self.source_id)

        self.id: str = meta.get("imdb_id", "") or meta.get("id", "")
        self.name_zh: str = meta.get("name_zh", "") or ""
        self.name_en: str = meta.get("name_en", "") or ""
        self.title: str = meta.get("name", "") or self.name_zh or self.name_en
        self.type: str = meta.get("type", "")
        year_raw = meta.get("releaseInfo", "") or meta.get("year", "")
        self.year: int | None = self._parse_year(year_raw)
        self.videos: list[dict] = meta.get("videos", []) or []
        self.poster_path: str | None = meta.get("poster_path") or None
        self.backdrop_path: str | None = meta.get("backdrop_path") or None
        if image_base:
            self.IMAGE_BASE = image_base

    @staticmethod
    def _parse_year(raw) -> int | None:
        for token in str(raw).replace("–", "-").split("-"):
            token = token.strip()
            if token.isdigit() and 1900 <= int(token) <= 2100:
                return int(token)
        return None

    def poster_url(self, size: str = "w500") -> str | None:
        if not self.poster_path:
            return None
        return f"{self.IMAGE_BASE}/{size}{self.poster_path}"

    def backdrop_url(self, size: str = "w1280") -> str | None:
        if not self.backdrop_path:
            return None
        return f"{self.IMAGE_BASE}/{size}{self.backdrop_path}"

    def episode_title(self, season: int, episode: int) -> str | None:
        for v in self.videos:
            if v.get("season") == season and v.get("episode") == episode:
                return v.get("name") or v.get("title")
        return None


class MetaProvider(ABC):
    """元数据提供者抽象基类。

    派生类（TMDB、豆瓣、OMDB……）实现 configured / fetch_meta / fetch_meta_by_keyword，
    工厂函数 get_meta_provider() 按 settings.META_PROVIDER 返回对应实例。"""

    #: 子类覆盖：配置项是否齐全可启用（admin 可配置 API key）
    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    async def fetch_meta(self, source_id: str, vtype: str,
                         db_path: str | None = None,
                         source_kind: str = "imdb") -> VideoMeta | None:
        """按外部 ID 拉元数据。未收录 / 网络失败返回 None。

        source_kind:  source_id 的来源类型
          - "imdb":    IMDb ID（ttXXXXXXX，Stremio 默认传）
          - "tmdb":    TMDB 内部数字 ID（123456）
          - "keyword": 关键词（如 "星际穿越"），调用方知道自己传的是名字
          - 其他 provider 可扩展（如 "douban"、"omdb"）

        派生类必须在构造 VideoMeta 时正确填 source_id / source_kind。"""

    @abstractmethod
    async def fetch_meta_by_keyword(self, keyword: str, vtype: str,
                                    db_path: str | None = None) -> tuple[VideoMeta | None, str]:
        """独立搜索用：按关键词匹配 imdb_id + 完整 meta。
        返回 (meta, imdb_id)；都匹配不到返回 (None, '')。"""
