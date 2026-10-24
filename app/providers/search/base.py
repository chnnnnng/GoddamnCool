"""外部资源检索 Provider 抽象（阶段 3）。

与云盘 Provider（providers/base.py）平行：ResourceSearchProvider 面向
"外部资源索引站点"，按影片标题搜索可转存的网盘分享链接。接口刻意保持
最小（search/close），去重与评分在候选服务层完成，避免业务绑定到任何
单一搜索站点；单个来源失败不应影响其他来源。
"""
from dataclasses import dataclass


@dataclass
class SearchCandidate:
    """一条可转存的网盘分享资源候选。"""

    title: str           # 资源标题（索引站点的描述文本）
    share_url: str       # 分享链接（Provider 已做格式校验）
    passcode: str = ""   # 提取码（无则空）
    source: str = ""     # 来源描述，如 "plugin:mizixing"、"tg:频道名"
    published: str = ""  # 资源发布时间（ISO 字符串，未知为空）
    provider: str = ""   # 产出该候选的 Provider 名
    disk: str = "quark"  # 网盘类型（quark/uc/baidu/aliyun/xunlei/115/123/tianyi…）
    score: float = 0.0   # 匹配评分（候选服务层填充，仅用于排序）
    alive_state: str = ""    # 链接存活检测状态（ok/bad/locked/unsupported/uncertain；非夸克留空）
    alive_summary: str = ""  # 检测说明文本（如"链接有效"/"分享不存在"）


class SearchError(Exception):
    """搜索来源不可用（超时/限流/接口变化/响应异常）。"""


class ResourceSearchProvider:
    """外部资源索引接口（阶段 3 仅 PanSou 实现，可插拔扩展）。"""

    name: str = "abstract"

    async def search(self, keyword: str, limit: int = 20,
                     cloud_types: list[str] | None = None) -> list[SearchCandidate]:
        """按关键词搜索资源，返回去重后的候选列表。失败抛 SearchError。

        cloud_types: 限定网盘类型；None → Provider 默认（夸克），
        空列表 → 不限类型搜全部（每条候选带 disk 标签）。
        """
        raise NotImplementedError

    async def close(self) -> None:
        """释放底层连接。"""
        raise NotImplementedError
