"""代理流端点测试：签名校验、Stream 对象代理模式。"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DB_PATH", "/tmp/quark_stremio_test.db")
os.environ.setdefault("PROXY_STREAM", "1")
os.environ.setdefault("BASE_URL", "http://addon.example.test")

from fastapi.testclient import TestClient  # noqa: E402

import app.proxy as proxy_mod  # noqa: E402
import app.stremio_routes as stremio_routes  # noqa: E402
import app.stream_service as ss  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.base import CloudFile  # noqa: E402

from tests.test_stremio_api import FakeProvider, patch_service  # noqa: E402

client = TestClient(app)
client.__enter__()


# ---------- 签名 ----------

def test_proxy_sign_roundtrip():
    url = proxy_mod.make_proxy_url("fid123")
    assert url.startswith("http://addon.example.test/proxy/fid123?")
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(url).query)
    assert proxy_mod.verify_proxy_url("fid123", q["e"][0], q["s"][0])
    assert not proxy_mod.verify_proxy_url("fidXXX", q["e"][0], q["s"][0])
    assert not proxy_mod.verify_proxy_url("fid123", "123", q["s"][0])  # 过期/篡改


def test_proxy_endpoint_rejects_bad_signature():
    r = client.get("/proxy/fid123?e=9999999999&s=badbadbad")
    assert r.status_code == 403


# ---------- Stream 对象代理模式 ----------

def _files():
    return [CloudFile(fid="f1", name="The.Avengers.2012.1080p.BluRay.x264.mkv",
                      size=2048, is_dir=False)]


def _meta():
    return {"imdb_id": "tt0848228", "name": "The Avengers",
            "type": "movie", "releaseInfo": "2012"}


def test_stream_uses_proxy_url(monkeypatch):
    provider = FakeProvider(_files())
    svc = patch_service(monkeypatch, provider, _meta())
    monkeypatch.setattr(stremio_routes, "service", svc)

    r = client.get("/stream/movie/tt0848228.json")
    s = r.json()["streams"][0]
    assert s["url"].startswith("http://addon.example.test/proxy/f1?")
    assert "proxyHeaders" not in s["behaviorHints"]
    assert s["behaviorHints"]["notWebReady"] is False


def test_stream_direct_mode_without_proxy(monkeypatch):
    provider = FakeProvider(_files())
    svc = patch_service(monkeypatch, provider, _meta())
    monkeypatch.setattr(ss.config, "PROXY_STREAM", False)
    monkeypatch.setattr(stremio_routes, "service", svc)

    r = client.get("/stream/movie/tt0848228.json")
    s = r.json()["streams"][0]
    assert s["url"] == "https://dl.example/x.mp4"
    assert s["behaviorHints"]["notWebReady"] is True
    assert "Cookie" in s["behaviorHints"]["proxyHeaders"]["request"]
