"""Web 授权页：粘贴网盘 Cookie 完成绑定（阶段 3 升级为正式配置页）。"""
import html

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import config, db
from .providers.quark import QuarkDriveProvider
from .providers.base import ProviderError
from .stream_service import StreamService

router = APIRouter()
service = StreamService()

_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; color: #222; }}
  .card {{ border: 1px solid #e3e3e3; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.04); }}
  h1 {{ font-size: 20px; }} code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 4px; }}
  textarea, input {{ width: 100%; box-sizing: border-box; margin: 8px 0 16px; padding: 10px;
       border: 1px solid #ccc; border-radius: 8px; font-size: 14px; }}
  textarea {{ height: 120px; word-break: break-all; }}
  button {{ background: #7b5cff; color: #fff; border: 0; border-radius: 8px;
       padding: 10px 24px; font-size: 15px; cursor: pointer; }}
  button:disabled {{ opacity: .5; }}
  .msg {{ margin: 12px 0; padding: 10px 14px; border-radius: 8px; display: none; }}
  .ok {{ background: #e8f8ee; color: #177245; display: block; }}
  .err {{ background: #fdecec; color: #b3261e; display: block; }}
  ol {{ padding-left: 20px; line-height: 1.7; }}
</style>
</head>
<body>
<div class="card">
  <h1>{title} · 账号绑定</h1>
  <p>本服务为<b>单用户自托管</b>。绑定后，Stremio 将使用您个人网盘内已保存的影视资源。</p>
  <ol>
    <li>电脑浏览器登录 <code>pan.quark.cn</code></li>
    <li>按 <code>F12</code> 打开开发者工具 → Network（网络）</li>
    <li>刷新页面，点击任意发往 <code>quark.cn</code> 的请求</li>
    <li>在 Request Headers 中右键复制完整 <code>Cookie</code> 值</li>
  </ol>
  <form method="post" action="/setup" onsubmit="btn.disabled=true;document.getElementById('busy').style.display='block'">
    <label>访问令牌（ADMIN_TOKEN）</label>
    <input name="token" type="password" required placeholder="服务器 .env 中配置的 ADMIN_TOKEN">
    <label>夸克网盘 Cookie</label>
    <textarea name="cookie" required placeholder="kps=...; sign=...; vcode=...; __puus=...;"></textarea>
    <button id="btn" type="submit">验证并绑定</button>
    <span id="busy" style="display:none">验证中…</span>
  </form>
  <div id="msg" class="msg"></div>
</div>
</body></html>"""


@router.get("/setup", response_class=HTMLResponse)
async def setup_page():
    return HTMLResponse(_PAGE.format(title=config.ADDON_NAME))


@router.post("/setup")
async def setup_submit(request: Request, token: str = Form(...), cookie: str = Form(...)):
    if token != config.ADMIN_TOKEN:
        return _result(False, "访问令牌错误", 403)
    cookie = cookie.strip().strip('"').strip("'")
    if "__puus" not in cookie:
        return _result(False, "Cookie 不完整（缺少 __puus 字段），请重新完整复制", 400)
    provider = QuarkDriveProvider(cookie=cookie, db_path=config.DB_PATH)
    try:
        await provider.verify()
    except ProviderError as e:
        return _result(False, f"Cookie 验证失败：{e}", 400)
    finally:
        await provider.close()
    db.save_cookie(config.DB_PATH, cookie)
    return _result(True, "绑定成功！请在 Stremio 中安装/刷新 Add-on", 200)


def _result(ok: bool, message: str, code: int):
    page = _PAGE.format(title=config.ADDON_NAME).replace(
        '<div id="msg" class="msg"></div>',
        f'<div id="msg" class="msg {"ok" if ok else "err"}">{html.escape(message)}</div>')
    return HTMLResponse(page, status_code=code)


@router.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "version": config.ADDON_VERSION,
        "cookie_bound": service.load_cookie() is not None,
    })
