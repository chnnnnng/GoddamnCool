"""向后兼容门面层——固定走 TMDB provider。

所有 TMDB 实现已迁入 app/meta/tmdb.py 中的 TMDBMetaProvider 派生类。
本模块的 async 函数（fetch_meta / fetch_meta_by_keyword / configured）固定走
``tmdb_provider`` 单例（不走 settings.META_PROVIDER 切换），保持原有 import 路径
对所有旧代码零改动。

新代码要做跨 provider 切换，请用 ``from app.meta import get_meta_provider``。

模块级内部属性（_host / _host_ts / _api_get / _fetch_ip / _find / _tv_episodes）
通过 ``__getattr__`` 代理到 tmdb_provider 实例；``__setattr__`` 无法在 Python 模块
上拦截，因此测试 monkeypatch ``app.tmdb`` 模块的内部属性时，需直接 patch
``app.tmdb.tmdb_provider._host`` 等实例属性。"""
from __future__ import annotations

import logging

from .meta.base import VideoMeta
from .meta.tmdb import TMDBMetaProvider

log = logging.getLogger(__name__)

# 固定的 TMDB 单例（门面层不走 settings.META_PROVIDER 切换）
tmdb_provider = TMDBMetaProvider()


async def fetch_meta(imdb_id: str, vtype: str,
                     db_path: str | None = None,
                     transport=None) -> VideoMeta | None:
    return await tmdb_provider.fetch_meta(imdb_id, vtype, db_path, transport=transport)


async def fetch_meta_by_keyword(keyword: str, vtype: str,
                                db_path: str | None = None) -> tuple[VideoMeta | None, str]:
    return await tmdb_provider.fetch_meta_by_keyword(keyword, vtype, db_path)


def configured() -> bool:
    return tmdb_provider.configured


# ---------- 模块级属性代理：测试 / 调试用 ----------
# __getattr__ 让 tmdb_mod._host / tmdb_mod._fetch_ip 等可读；
# 测试 monkeypatch.setattr(tmdb_mod, "_host", "") 直接作用在 tmdb_provider 实例上
# 需要手动 setattr(tmdb_mod.tmdb_provider, "_host", "")（因为 Python 模块没有 __setattr__）。

def __getattr__(name: str):
    if name in ("_host", "_host_ts", "_fetch_ip", "_api_get", "_get",
                "_find", "_tv_episodes"):
        return getattr(tmdb_provider, name)
    raise AttributeError(f"module 'app.tmdb' has no attribute {name!r}")


__all__ = [
    "VideoMeta", "fetch_meta", "fetch_meta_by_keyword", "configured",
    "tmdb_provider",
]
