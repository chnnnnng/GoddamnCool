"""外部资源检索 Provider 包。"""
from .base import ResourceSearchProvider, SearchCandidate, SearchError
from .pansou import PanSouProvider, SHARE_URL_RE

__all__ = ["ResourceSearchProvider", "SearchCandidate", "SearchError",
           "PanSouProvider", "SHARE_URL_RE"]
