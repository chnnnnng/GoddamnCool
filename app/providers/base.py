"""云盘 Provider 抽象。阶段 3 外部检索等新数据源也应实现此接口。"""
from dataclasses import dataclass, field


@dataclass
class CloudFile:
    fid: str
    name: str
    size: int
    is_dir: bool
    parent_path: str = ""


@dataclass
class PlayResource:
    """一个可直接（或带头）播放的资源描述，最终映射为 Stremio Stream。"""
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    fid: str = ""  # 源文件标识（代理流回源用）
    filename: str = ""
    video_size: int = 0
    source_name: str = ""  # 展示名，如 "夸克网盘"


@dataclass
class ShareFile:
    """分享目录中的一个条目（阶段 4 转存用）。"""
    fid: str
    name: str
    size: int
    is_dir: bool
    fid_token: str = ""       # share_fid_token，转存提交必带
    path: str = ""            # 分享内相对路径（诊断展示用）


class CloudDriveProvider:
    """个人网盘提供方接口（阶段 2 仅夸克实现）。"""

    name: str = "abstract"

    async def verify(self) -> None:
        """校验登录态，失败抛 ProviderAuthError。"""
        raise NotImplementedError

    async def search_files(self, keyword: str, limit: int = 50) -> list[CloudFile]:
        """全盘搜索文件（不含目录），供匹配器筛选。"""
        raise NotImplementedError

    async def get_play_resource(self, file: CloudFile) -> PlayResource:
        """获取文件的播放资源（直链+所需请求头，或无需头的流媒体直链）。"""
        raise NotImplementedError

    # ---- 分享转存（阶段 4；仅夸克实现，接口形态为非官方私有接口）----

    async def get_share_stoken(self, pwd_id: str, passcode: str = "") -> str:
        """用分享 ID + 提取码换取 stoken。分享失效/提取码错误抛 ProviderError。"""
        raise NotImplementedError

    async def list_share_files(self, pwd_id: str, stoken: str,
                               max_depth: int = 3) -> list[ShareFile]:
        """遍历分享目录树，返回全部条目（文件与目录，均带 fid_token）。"""
        raise NotImplementedError

    async def save_share_files(self, pwd_id: str, stoken: str,
                               items: list[ShareFile],
                               to_pdir_fid: str = "0") -> dict:
        """提交转存到 to_pdir_fid 目录，返回 {"task_id", "top_fids"}
        （顶层落盘 fid，可能为空列表）。"""
        raise NotImplementedError

    async def get_transfer_task(self, task_id: str, retry_index: int = 0) -> dict:
        """查询转存任务：{"status": int(2=成功), "top_fids": [...], "message": str}。"""
        raise NotImplementedError

    async def find_or_create_dir(self, name: str) -> str:
        """在网盘根目录定位同名文件夹，不存在则创建，返回 fid。"""
        raise NotImplementedError

    async def close(self) -> None:
        """释放底层连接。"""
        raise NotImplementedError


class ProviderError(Exception):
    """网盘访问通用错误。"""


class ProviderAuthError(ProviderError):
    """登录态失效/未授权，应提示用户重新绑定。"""
