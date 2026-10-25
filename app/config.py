"""环境配置。所有可调项集中在 .env / 环境变量，代码不硬编码凭证。"""
import os
from pathlib import Path


def _load_dotenv() -> None:
    """极简 .env 加载器（避免引入 python-dotenv 依赖）。已有环境变量优先。"""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

SECRET_KEY: str = os.environ.get("SECRET_KEY", "")
ADMIN_TOKEN: str = os.environ.get("ADMIN_TOKEN", "")
DB_PATH: str = os.environ.get("DB_PATH", "data/quark_stremio.db")
HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8000"))
MATCH_THRESHOLD: float = float(os.environ.get("MATCH_THRESHOLD", "0.6"))
# 对外可访问的基础地址（生成 Add-on 地址与引导链接用），如 http://47.117.135.164
BASE_URL: str = os.environ.get("BASE_URL", "").rstrip("/")
# 前端静态资源目录（前后端分离：HTML/CSS/JS 为独立文件，后端仅提供 JSON API 与静态服务）
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

ADDON_ID = "community.quark.stremio"
ADDON_VERSION = "0.2.0"
# 产品名称：页面标题 / 顶栏品牌 / Stremio Add-on 名称统一使用（.env APP_NAME 可改）
APP_NAME: str = os.environ.get("APP_NAME", "GoddamnCool!")
ADDON_NAME = APP_NAME
ADDON_DESCRIPTION = "在 Stremio 中播放您个人夸克网盘内的影视资源（仅限自有内容）"
ADDON_CONTACT_EMAIL = ""
# Stremio Add-on 图标 URL（.env 可设默认；管理员可在 /admin 在线修改）
ADDON_ICON: str = os.environ.get("ADDON_ICON", "")

def _read_root_file(name: str) -> str:
    """读取项目根目录下的文本文件（去空白）；不存在返回空串。"""
    try:
        return (Path(__file__).resolve().parent.parent / name).read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""


# 元数据（TMDB）：API key 优先取环境变量，其次项目根目录的 TMDB_API_KEY 文件
TMDB_API_KEY: str = os.environ.get("TMDB_API_KEY", "") or _read_root_file("TMDB_API_KEY")
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_API_HOST = "api.themoviedb.org"  # IP 直连时的 SNI/Host 头/证书校验域名
# 双语名/季集缓存有效期（秒）；连载剧集 7 天后自动刷新
TMDB_META_TTL: float = float(os.environ.get("TMDB_META_TTL", str(7 * 86400)))
# CheckTMDB（github.com/cnwikee/CheckTMDB）：每日更新的 TMDB 可用 IP hosts 表，
# 解决境内 api.themoviedb.org DNS 污染。github raw 境内常超时，走 jsdelivr CDN。
CHECKTMDB_HOSTS_URL = ("https://cdn.jsdelivr.net/gh/cnwikee/CheckTMDB@main/"
                       "Tmdb_host_ipv4")

# 外部资源检索（阶段 3）：PanSou 聚合搜索实例（github.com/fish2018/pansou，MIT）。
# 默认公开实例，可替换为自部署地址；搜索为 JSON 文本级流量，几乎无带宽开销。
PANSOU_BASE_URL: str = os.environ.get("PANSOU_BASE_URL", "https://so.252035.xyz").rstrip("/")
SEARCH_TIMEOUT: float = float(os.environ.get("SEARCH_TIMEOUT", "25"))
SEARCH_LIMIT: int = int(os.environ.get("SEARCH_LIMIT", "30"))
# Lite 用户每日搜索次数上限（Pro/管理员不限；管理员可在 /admin 页面在线修改）
LITE_DAILY_SEARCH_LIMIT: int = int(os.environ.get("LITE_DAILY_SEARCH_LIMIT", "50"))
# Lite 用户每日转存次数上限（Pro/管理员不限；管理员可在 /admin 页面在线修改）
LITE_DAILY_TRANSFER_LIMIT: int = int(os.environ.get("LITE_DAILY_TRANSFER_LIMIT", "10"))
# Lite 用户每日 Stremio 在线播放次数上限（Pro/管理员不限；管理员可在 /admin 页面在线修改）
LITE_DAILY_STREAMING_LIMIT: int = int(os.environ.get("LITE_DAILY_STREAMING_LIMIT", "50"))
# 搜索结果与分享详情缓存有效期（秒，默认 1 天）；0 关闭缓存
SEARCH_CACHE_TTL: float = float(os.environ.get("SEARCH_CACHE_TTL", str(86400)))
# 搜索后对"有效"候选做内容解析的最大条数
INSPECT_TOP_N: int = int(os.environ.get("INSPECT_TOP_N", "12"))
MIN_VIDEO_SIZE_H: float = float(os.environ.get("MIN_VIDEO_SIZE_H", "0.5"))

# 大语言模型（可选高级功能）：OpenAI 兼容 API，管理员在 /admin 在线配置；
# 三项全部非空才启用（AI 精选/文件名兜底解析），留空自动退回基础功能
LLM_API_BASE: str = os.environ.get("LLM_API_BASE", "")
LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "")
LLM_MODEL: str = os.environ.get("LLM_MODEL", "")
# AI 两阶段精选参数：SKIM 阶段最多送几个 alive 候选给 AI（按匹配度降序）；
# DEEP PARSE 阶段最多解析几个 keep=true 候选（按 AI 评分降序）
LLM_SKIM_TOP_N: int = int(os.environ.get("LLM_SKIM_TOP_N", "10"))
LLM_DEEP_TOP_N: int = int(os.environ.get("LLM_DEEP_TOP_N", "3"))

# 转存 Worker（阶段 4）：目标目录、扫描/轮询节奏、超时与重试上限
TRANSFER_DIR: str = os.environ.get("TRANSFER_DIR", "GODDAMNCOOL")
TRANSFER_SCAN_INTERVAL: float = float(os.environ.get("TRANSFER_SCAN_INTERVAL", "10"))
TRANSFER_POLL_INTERVAL: float = float(os.environ.get("TRANSFER_POLL_INTERVAL", "2"))
TRANSFER_TASK_TIMEOUT: float = float(os.environ.get("TRANSFER_TASK_TIMEOUT", "60"))
TRANSFER_MAX_ATTEMPTS: int = int(os.environ.get("TRANSFER_MAX_ATTEMPTS", "2"))


def validate() -> list[str]:
    errors = []
    if not SECRET_KEY:
        errors.append("SECRET_KEY 未设置（生成方式见 .env.example）")
    if not ADMIN_TOKEN:
        errors.append("ADMIN_TOKEN 未设置（用作注册邀请码）")
    return errors
