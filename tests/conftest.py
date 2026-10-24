"""测试全局配置：必须在任何 app 模块导入前设置环境变量。"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("SECRET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DB_PATH", "/tmp/quark_stremio_test.db")
os.environ.setdefault("PROXY_STREAM", "1")
os.environ.setdefault("BASE_URL", "http://addon.example.test")
