"""测试全局配置：必须在任何 app 模块导入前设置环境变量。"""
import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("SECRET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DB_PATH", "/tmp/quark_stremio_test.db")
os.environ.setdefault("PROXY_STREAM", "1")
os.environ.setdefault("BASE_URL", "http://addon.example.test")

from app import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """settings 进程内缓存是模块级共享状态：每个用例前清空，
    防止某个用例写入的运行时配置泄漏影响后续用例。"""
    settings._cache = {}
    yield
    settings._cache = {}
