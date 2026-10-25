"""运行时可编辑配置：管理员在 /admin 页面修改，存 DB settings 表并热生效。

优先级：DB settings > config 默认值（环境变量）。进程内缓存避免每请求查库，
管理员保存后立即刷新缓存。白名单外的键拒绝写入。
"""
import logging

from . import config, db

log = logging.getLogger(__name__)

# 可编辑配置白名单：键 -> 类型（bool 不允许；数值需 >= 0）
EDITABLE: dict[str, type] = {
    "APP_NAME": str,                # 产品名（页面标题/品牌/Stremio Add-on 名）
    "ADDON_ID": str,                # Stremio Add-on 唯一标识
    "ADDON_DESCRIPTION": str,       # Stremio Add-on 描述
    "ADDON_ICON": str,              # Stremio Add-on 图标 URL
    "LITE_DAILY_SEARCH_LIMIT": int, # Lite 用户每日搜索次数上限
    "LITE_DAILY_TRANSFER_LIMIT": int,  # Lite 用户每日转存次数上限
    "SEARCH_LIMIT": int,            # 单次搜索返回候选数上限
    "INSPECT_LIMIT": int,           # 搜索后解析分享详情的最大条数
    "SEARCH_CACHE_TTL": int,        # 搜索结果缓存秒数（0 关闭）
    "MATCH_THRESHOLD": float,       # 文件名匹配阈值
    "TRANSFER_DIR": str,            # 转存目标目录名（网盘根目录下）
    "PANSOU_BASE_URL": str,         # PanSou 聚合搜索地址
    "TMDB_API_KEY": str,            # TMDB API Key（管理员统一填写，用户不可见）
    "LLM_API_BASE": str,            # 大语言模型 API 地址（OpenAI 兼容，留空禁用 AI 功能）
    "LLM_API_KEY": str,             # 大语言模型 API Key（管理员统一填写，回显打码）
    "LLM_MODEL": str,               # 大语言模型名称（如 gpt-4o-mini / qwen-plus）
}

# 允许留空的字符串配置（留空含义见 META）
_ALLOW_EMPTY = {"TRANSFER_DIR", "PANSOU_BASE_URL", "TMDB_API_KEY",
                "ADDON_DESCRIPTION", "ADDON_ICON",
                "LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"}

# 管理页展示用中文说明
SETTINGS_META: dict[str, str] = {
    "APP_NAME": "产品名称（页面标题 / 顶栏品牌 / Stremio Add-on 名）",
    "ADDON_ID": "Stremio Add-on 唯一标识（修改后 Stremio 会视为全新 Add-on，需重新安装）",
    "ADDON_DESCRIPTION": "Stremio Add-on 描述（留空用默认文案）",
    "ADDON_ICON": "Stremio Add-on 图标 URL（留空不显示图标）",
    "LITE_DAILY_SEARCH_LIMIT": "Lite 用户每日搜索次数上限（Pro 不限）",
    "LITE_DAILY_TRANSFER_LIMIT": "Lite 用户每日转存次数上限（Pro 不限）",
    "SEARCH_LIMIT": "单次搜索返回的候选数量上限",
    "INSPECT_LIMIT": "搜索后自动解析分享内容详情的最大条数",
    "SEARCH_CACHE_TTL": "搜索结果缓存有效期（秒，0 表示关闭缓存）",
    "MATCH_THRESHOLD": "转存时文件名与影片名的匹配阈值（0~1）",
    "TRANSFER_DIR": "默认转存目标目录名（网盘根目录下的文件夹，留空存根目录；用户可在面板自定义）",
    "PANSOU_BASE_URL": "PanSou 聚合搜索服务地址（留空禁用外部搜索）",
    "TMDB_API_KEY": "TMDB API Key（32 位十六进制，管理员统一填写，普通用户不可见）",
    "LLM_API_BASE": "大语言模型 API 地址（OpenAI 兼容，如 https://api.openai.com/v1，"
                    "留空禁用 AI 精选/文件名解析等高级功能）",
    "LLM_API_KEY": "大语言模型 API Key（管理员统一填写，保存后回显打码，普通用户不可见）",
    "LLM_MODEL": "大语言模型名称（如 gpt-4o-mini / qwen-plus / deepseek-chat）",
}

_cache: dict = {}


def load(db_path: str) -> None:
    """启动时把 DB 内的覆盖值读入缓存。"""
    global _cache
    _cache = db.all_settings(db_path)
    if _cache:
        log.info("运行时配置覆盖: %s", {k: v for k, v in _cache.items()})


def get(key: str):
    """读取配置：DB 覆盖值优先，否则 config 默认值。键必须在白名单内。"""
    if key not in EDITABLE:
        raise KeyError(f"未知配置项: {key}")
    if key in _cache:
        return _cache[key]
    return getattr(config, key)


def set(db_path: str, updates: dict) -> dict:
    """批量写入（白名单 + 类型校验），返回生效后的完整配置。非法键/值抛 ValueError。"""
    checked: dict = {}
    for key, value in updates.items():
        if key not in EDITABLE:
            raise ValueError(f"不支持的配置项: {key}")
        typ = EDITABLE[key]
        if typ is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} 必须是整数")
        elif typ is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} 必须是数字")
        else:
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须是字符串")
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError(f"{key} 不能为负数")
        if typ is str and key not in _ALLOW_EMPTY and not value.strip():
            raise ValueError(f"{key} 不能为空")
        checked[key] = value
    for key, value in checked.items():
        db.set_setting(db_path, key, value)
        _cache[key] = value
    log.info("配置已更新: %s", checked)
    return snapshot()


def snapshot() -> dict:
    """当前生效的全部白名单配置（管理页回显用）。"""
    return {key: get(key) for key in EDITABLE}
