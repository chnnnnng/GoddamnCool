"""搜索域：PanSou Provider + 搜索链 + 候选服务。"""
from .base import ResourceSearchProvider, SearchCandidate, SearchError
from .pansou import PanSouProvider, SHARE_URL_RE
from .search_service import SearchService
from .search_chains import BaseSearchChain, AISearchChain

__all__ = [
    "ResourceSearchProvider", "SearchCandidate", "SearchError",
    "PanSouProvider", "SHARE_URL_RE",
    "SearchService",
    "BaseSearchChain", "AISearchChain",
]
