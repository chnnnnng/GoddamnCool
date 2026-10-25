"""元数据源工厂。

用法 1（面向 provider 抽象）::

    from app.meta import get_meta_provider
    provider = get_meta_provider()       # 返回当前 settings.META_PROVIDER 对应实例
    meta = await provider.fetch_meta("tt5636724", "series")

用法 2（向后兼容，所有旧 import 不变）::

    from app.meta import VideoMeta, fetch_meta, fetch_meta_by_keyword

配置项 ``META_PROVIDER`` 管理员可在 /admin 页面切换，默认 ``"tmdb"``。
新增 provider 只需：

1. 在 meta/ 下加派生类（继承 MetaProvider）
2. 在 ``get_meta_provider`` 的注册表中登记一个字符串键
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import MetaProvider, VideoMeta

if TYPE_CHECKING:  # pragma: no cover
    import httpx

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[MetaProvider]] = {}


def _register(name: str):
    def deco(cls):
        _REGISTRY[name] = cls
        return cls
    return deco


def _build_registry():
    """懒加载所有内置 provider，避免顶层循环 import。"""
    from . import tmdb as _tmdb_mod
    _register("tmdb")(_tmdb_mod.TMDBMetaProvider)


_build_registry()


_cached_provider: MetaProvider | None = None
_cached_key: str = ""


def get_meta_provider() -> MetaProvider:
    """返回当前 settings.META_PROVIDER 配置的 provider 单例（进程内缓存）。

    配置项改变时自动重建实例。未知 provider 回退到 tmdb。"""
    global _cached_provider, _cached_key
    from .. import settings as _stg
    key = (_stg.get("META_PROVIDER") or "tmdb").lower()
    if key != _cached_key or _cached_provider is None:
        cls = _REGISTRY.get(key) or _REGISTRY["tmdb"]
        if key not in _REGISTRY:
            log.warning("META_PROVIDER=%s 未注册，回退 tmdb", key)
        _cached_provider = cls()
        _cached_key = key
    return _cached_provider


# ---------- 便捷函数：直接走当前 provider ----------

async def fetch_meta(imdb_id: str, vtype: str,
                     db_path: str | None = None,
                     transport=None) -> VideoMeta | None:
    return await get_meta_provider().fetch_meta(imdb_id, vtype, db_path, transport=transport)


async def fetch_meta_by_keyword(keyword: str, vtype: str,
                                db_path: str | None = None) -> tuple[VideoMeta | None, str]:
    return await get_meta_provider().fetch_meta_by_keyword(keyword, vtype, db_path)


def configured() -> bool:
    return get_meta_provider().configured


__all__ = [
    "VideoMeta", "MetaProvider",
    "get_meta_provider", "fetch_meta", "fetch_meta_by_keyword", "configured",
]
