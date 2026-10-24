"""应用入口：python -m app.main 启动（HOST/PORT 见 .env）。"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import config, db, settings
from .admin_routes import router as admin_router
from .auth_routes import router as auth_router
from .candidate_routes import router as candidate_router
from .stremio_routes import router as stremio_router
from .transfer_worker import TransferWorker
from .user_routes import router as user_router
from .web_routes import router as web_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="Quark Stremio Add-on", version=config.ADDON_VERSION,
              docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    expose_headers=["X-Addon-Status"],
)


class NoStoreStaticFiles(StaticFiles):
    """静态资源禁止缓存：管理页前端演进快，防浏览器用旧 JS 调新 API。"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store"
        return resp


app.include_router(stremio_router)
app.include_router(web_router)
app.include_router(candidate_router)
app.include_router(auth_router)
app.include_router(user_router)
app.include_router(admin_router)
# 前端静态资源（前后端分离）：HTML 页面由路由以 FileResponse 提供（no-store），
# CSS/JS 经 /static 挂载提供。
app.mount("/static", NoStoreStaticFiles(directory=config.WEB_DIR), name="static")

worker = TransferWorker()


@app.on_event("startup")
async def startup():
    errors = config.validate()
    if errors:
        raise RuntimeError("配置缺失: " + "; ".join(errors))
    db.init(config.DB_PATH, config.SECRET_KEY)
    settings.load(config.DB_PATH)  # 运行时可编辑配置（DB 覆盖值）入缓存
    worker.start()  # 转存 Worker：扫描 PENDING 并恢复重启前的未完成任务


@app.on_event("shutdown")
async def shutdown():
    await worker.stop()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")
