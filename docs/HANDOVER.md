# GoddamnCool! 项目交接文档

> **夸克网盘 × Stremio 自托管 Add-on**：绑定个人夸克 Cookie → Stremio 直接播网盘文件 → 未命中时搜 PanSou → 一键转存 → Stremio 刷新即播。
> 本文档面向接手的工程师 / AI Agent。所有命令已实测通过。

---

## 1. 一句话速览

| 项 | 值 |
|---|---|
| 技术栈 | Python 3.14 + FastAPI + httpx + SQLite + Fernet |
| 监听 | 公网 `47.117.135.164:80`，进程 PID 查 `ss -tlnp \| grep ':80' \| grep -oP 'pid=\K[0-9]+' \| head -1` |
| 启动 | `cd /root/workspace && setsid nohup .venv/bin/python -m app.main > /tmp/app.log 2>&1 < /dev/null &` |
| 测试 | `cd /root/workspace && .venv/bin/python -m pytest -q` → **245 passed** |
| DB | `/root/workspace/data/quark_stremio.db`（Fernet 加密 Cookie） |
| 生产日志 | `/tmp/app.log`（服务 stdout/stderr） |
| commit 身份 | `git -c user.name=chnnnng -c user.email=chnnnng@example.com commit -m "..."` 只提交不推 |
| 产品名 | `GoddamnCool!`（`APP_NAME` 配置项可改） |
| 外部依赖 | TMDB API（`api.tmdb.org`，走 Cloudflare IPv6 直连）、PanSou（自部署 `127.0.0.1:8888`） |
| 前端 | `web/` 原生 HTML/CSS/JS，无构建步骤，`/static` 挂载全部 `no-store` |

### 功能阶段（全部已落地）

| 阶段 | 内容 |
|---|---|
| 1-2 | 夸克 Provider + 播放直链 + Stremio stream 对接 + 单用户 Cookie 绑定 |
| 3 | PanSou 外部检索、候选页、双名搜索、存活检测 |
| 4 | 转存 Worker 六态状态机、防重、重启恢复 |
| 5 | 分享详情 inspect、整包 / 单集 / 多选转存、规格解析 |
| 6 | 独立搜索页 /search、多网盘筛选、TV 遥控器适配 |
| 7 | 搜索缓存 1 天、弹窗多选转存、深色绿色硬朗 UI、Admin 面板可配置 |
| 7+ | **多用户架构**（`users` 表 + `stream_token`）、MetaProvider 抽象（TMDB / 可扩展豆瓣）、TMDB 默认 host 改 `api.tmdb.org` |

---

## 2. 最新目录结构

```
app/                             # 后端
├── main.py                      # FastAPI 组装（路由注册、/static 挂载、Worker 启动、DB init）
├── config.py                    # 全部硬编码常量（环境变量 + .env + 根目录文件加载）
├── settings.py                  # Admin 面板可配置项（SQLite kv 表，支持 bool/int/float/str）
├── db.py                        # SQLite 全部表的建表 / CRUD / 加密（Fernet）
├── auth.py                      # Fernet 密钥、加密 / 解密 Cookie
├── llm.py                       # AI 评审配置（API_BASE / API_KEY / MODEL，三项全非空才 configured()）
│
├── meta/                        # 元数据源域
│   ├── base.py                  # MetaProvider 抽象 + VideoMeta 统一数据类（含 source_id/source_kind/canonical_id）
│   ├── tmdb.py                  # TMDBMetaProvider 派生类（IMDb/TMDB ID/关键词三路径 + SQLite 缓存）
│   └── __init__.py              # get_meta_provider() 工厂（按 settings.META_PROVIDER 选实例）
├── search/                      # 搜索域
│   ├── base.py                  # ResourceSearchProvider / SearchCandidate / SearchError 抽象
│   ├── pansou.py                # PanSouProvider 实现（PanSou 实例搜索 + /api/check/links）
│   ├── search_service.py        # 搜索编排（多关键词并发、去重、评分、缓存 1 天）
│   ├── search_chains.py         # BaseSearchChain / AISearchChain（AI 链叠加 REVIEW → DEEPPARSE）
│   └── __init__.py              # 工厂 + SHARE_URL_RE 正则
├── search_routes.py             # 搜索相关 HTTP 路由（/api/candidates/*，需 X-Admin-Token）
│
├── providers/                   # 网盘 SDK 域
│   ├── base.py                  # CloudDriveProvider 抽象基类
│   ├── quark.py                 # 夸克私有接口封装（登录态、文件、分享、转存、play 直链）
│   └── __init__.py
│
├── stremio_routes.py            # Stremio Add-on 协议路由（/s/{stream_token}/manifest.json、/stream/...）
├── stream_service.py            # Stream 业务编排：IMDb ID → MetaProvider → 网盘匹配 → Stremio Stream
├── transfer_worker.py           # 转存 Worker（后台 asyncio 线程，六态状态机）
├── admin_routes.py              # Admin 面板 API（配置 / 用户 / 缓存 / API 调用统计）
├── user_routes.py               # 用户面板（生成 stream_token、绑定 Cookie、查看配额）
├── auth_routes.py               # 用户登录 / 注册
├── web_routes.py                # 页面路由（/setup、/search、/task/{id}，{{APP_NAME}} 注入）
├── matcher.py                   # MovieMatcher 文件名 ↔ 影片标题匹配（含拼音跨语言）
├── name_parser.py               # 文件名解析 + 规格徽章
└── __init__.py

web/                             # 前端静态文件（无构建，原生 HTML/CSS/JS）
├── index.html                   # /setup 账号绑定页
├── search.html                  # /search 搜索页（独立 + Stremio 锚定二合一）
├── task.html                    # /task/{id} 转存任务详情
├── admin.html                   # /admin 管理员面板
├── css/common.css               # 深色绿色主题（三页共用）
├── css/search.css               # 搜索页专属
├── css/admin.css                # Admin 面板
├── js/common.js                 # 令牌 / api() / DOM 工具（textContent 防 XSS）
├── js/spatial-nav.js            # TV 遥控器空间导航
├── js/setup.js search.js task.js admin.js
└── static/                      # 旧路径兼容（index.html 引用）

tests/                           # pytest（全部离线 Mock，不触外网）
scripts/
├── test_tmdb_latency.py         # TMDB 正确性 + 时延探测（api.tmdb.org 域名）
├── client_speedtest.sh / .ps1   # 客户端直链测速（Linux/macOS/Windows）
deploy/pansou/docker-compose.yml # PanSou 自部署
data/quark_stremio.db            # SQLite 数据库（运行时生成）
TMDB_API_KEY                     # 一行纯文本（运行时放置；或环境变量）
.env.example                     # 环境变量示例
```

---

## 3. 核心业务链路

### 3.1 Stremio Stream（主链路）

```
Stremio 客户端
  GET /s/{stream_token}/stream/{vtype}/{video_id}.json
       │                                     ↑
       │              video_id 形如 tt0816692 或 tt5636724:1:2
       ▼
  stremio_routes.user_stream()
    ├─ 查 users 表 → 用户 / Cookie / 配额
    └─ stream_service.get_streams(vtype, video_id, cookie, user_id)
         │
         ├─ parse_video_id() → (imdb_id, season, episode)
         ├─ MetaProvider.fetch_meta(imdb_id, vtype)
         │    └─ TMDB: /find/{imdb_id} zh+en gather
         │          → /{kind}/{tmdb_id}/external_ids 补拿 imdb_id
         │          → VideoMeta(title, name_zh, name_en, year, videos, poster)
         ├─ SearchService（当前用户的历史 PanSou 缓存 / 并发搜）
         │    候选排序 → MovieMatcher 置信度匹配
         │    置信度 ≥ MATCH_THRESHOLD 才返回
         ├─ transfer_tasks 表：查 COMPLETED 任务 → 直接短路定位文件
         └─ 返回 Stremio Stream 对象：
              ├─ 直链命中：url=play 直链 + proxyHeaders.Cookie
              └─ 未命中：externalUrl → /search?vtype=&id= 引导
```

### 3.2 搜索链（BaseSearchChain / AISearchChain）

```
BaseSearchChain（普通链）:
  PanSou.search()         → SearchCandidate 列表
  verify_links()          → /api/check/links 标注 alive_state
  inspect_content()       → /api/candidates/inspect 解析分享目录树
  评分排序 → ok/locked/bad 沉底
  写 kv_cache（1 天 TTL）

AISearchChain（AI 链，在普通链之上叠加）:
  普通链跑完整
  REVIEW(aka SKIM)  → LLM 概要甄选前 N 条
  DEEPPARSE(aka DEEP) → LLM 逐条详细解析 + 规格标注
  写 kv_cache（1 天 TTL）+ 加 ai_enhanced=True 标记
```

### 3.3 转存 Worker（六态状态机）

```
PENDING ──claim──▶ SUBMITTING ──提交成功──▶ PROCESSING ──轮询完成──▶ COMPLETED
                          │                          │
                          │ 永久错误                  │ 轮询超时
                          ▼                          ▼
                        FAILED                    EXPIRED
                          ▲
                          │ 瞬时错误重试 3 次
                          └──────────────────────────┘

防重：提交前 "文件名+大小" 搜网盘，命中跳过
定位不到目标文件 → 整包转存（宁占空间保播放）
重启恢复：SUBMITTING 无 quark_task_id → 重跑；有 → 续轮询
mode：episode(单集) / whole(整包) / picks(fids 多选)
```

### 3.4 MetaProvider 抽象

```python
# 当前唯一实现：TMDBMetaProvider（app/meta/tmdb.py）
# 未来可加：DoubanMetaProvider / OMDBMetaProvider

VideoMeta(
    source_id="tt0816692",       # 原始查询 ID
    source_kind="imdb",          # "imdb" / "tmdb" / "keyword"
    canonical_id="tt0816692",    # 统一主键（有 IMDb 就用它）
    id="tt0816692",              # 即 imdb_id
    name_zh="星际穿越", name_en="Interstellar",
    title="星际穿越", year=2014,
    type="movie", videos=[...],
    poster_path="/abc.jpg", backdrop_path="/def.jpg"
)

# 所有调用方统一走：
from app.meta import get_meta_provider
meta = await get_meta_provider().fetch_meta(source_id, vtype, source_kind="imdb")
```

**TMDB source_kind 三路径**：
- `"imdb"`：`/find/{imdb_id}` zh+en gather → 补调 `/{kind}/{id}/external_ids`（因为 /find 结果不带 imdb_id）
- `"tmdb"`：`/{kind}/{tmdb_id}` zh+en gather → 补调 external_ids
- `"keyword"`：委托给 `fetch_meta_by_keyword`

缓存 key 带前缀：`f"{source_kind}:{source_id}"`，TTL 7 天。

---

## 4. 夸克 / PanSou / TMDB 私有接口要点

### 4.1 夸克 Provider（`app/providers/quark.py`）

| 操作 | 端点 | 备注 |
|---|---|---|
| 文件搜索 | `GET /1/clouddrive/file/search` | 偶发 500，Provider 自动重试一次 |
| 播放直链 | `POST /1/clouddrive/file/v2/play` body `{"fid":"..."}` | 返回 `video_list`，取 `trans_status=="raw"` 为原画（SVIP 权益，非 SVIP 自动降级 super 1080p）；**批量 `/file/v2/play/project` 不可用**（14018/15000）|
| 分享 token | `POST /share/sharepage/token` | 失效=HTTP404+业务码41011；需提取码=41008 |
| 分享详情 | `POST /share/sharepage/detail` | 递归目录 |
| 转存提交 | `POST /share/sharepage/save` | |
| 转存轮询 | `GET /1/clouddrive/task?task_id=&retry_index=` | status 2=成功 |
| 创建目录 | `POST /file` | 同名返回 23008，需 find_or_create_dir |
| `__puus` | 每次响应 Set-Cookie 回写保存 | 风控/限流对抗关键 |

**坑**：
- CDN 签名覆盖完整 URI：直链**不能追加任何查询参数**（pr/fr 等），否则 403。
- 新转存文件 raw 异步出现（夸克后台扫描），Stream 先播转码 HLS（super/high），raw 出现后自动命中原画。
- **整包转存不做逐文件防重**：已有同名文件时夸克自动生成 `06(1).mkv` 副本（原件仍在，播放不受影响）。
- **下载直链 `/file/download`** 非取链 IP 限速 <100KB/s 且大文件 23018，**不要用**。

### 4.2 PanSou（搜索源）

- `POST /api/search` body `{"kw":"片名","res":"merge","cloud_types":["quark"]}`
- `POST /api/check/links` 批量存活检测（单次 ≤40 条，PanSou 服务端自带检测缓存）
- 公开实例波动大，**推荐自部署**（`deploy/pansou/docker-compose.yml`，端口 8888）。境内拉 ghcr.io 用南大镜像源 `ghcr.nju.edu.cn/fish2018/pansou-web:latest`。

### 4.3 TMDB（元数据）

- **默认 host = `api.tmdb.org`**（Cloudflare IPv6，不受 `api.themoviedb.org` 境内 DNS 污染）
- API key 优先环境变量，其次根目录 `TMDB_API_KEY` 文件
- `/search/{movie|tv}` 和 `/find/{imdb_id}` **返回结果里没有 `imdb_id` 字段**——必须补调 `/{kind}/{tmdb_id}/external_ids`
- `language` 参数默认 `en-US`，要中文标题传 `zh-CN`
- 图片路径是片段（`/abc.jpg`），完整 URL = `https://image.tmdb.org/t/p/w500{path}`

---

## 5. 多用户架构

```
users 表：
  id (UUID), username, password_hash, role (user/admin),
  tier (free/pro), stream_token (唯一，Add-on 地址),
  cookie (Fernet 加密), quota (pro 不限，free 每日 STREAMING_LIMIT 次),
  created_at, disabled

stream_token → 用户隔离：
  每个用户专属 Add-on 地址 /s/{token}/manifest.json + /s/{token}/stream/...
  Cookie / 任务 / 配额 / 搜索历史 全部按 user_id 隔离
  测试 monkeypatch search_service / transfer_worker / stream_service 共享同一个 SearchService 单例

SQLite 所有表按 user_id 或 stream_token 过滤查询
```

---

## 6. 数据模型（SQLite, `app/db.py`）

| 表 | 说明 |
|---|---|
| `users` | 多用户账号表（见上）|
| `kv` | 单行配置（Fernet 密钥等）+ Admin 面板可配置项（settings 模块读此表）|
| `transfer_tasks` | 转存任务。关键列：`status` (六态)、`video_type`（不是 vtype）、`imdb_id`（可为空=share-only）、`season`、`episode`、`mode` (episode/whole/picks)、`picks` (fid JSON)、`share_url`、`passcode`、`target_fid`、`result_json`（含 files 清单）、`error`、`attempts`、`quark_task_id`、幂等键 = share + imdb + IFNULL(季/集,-1) + picks |
| `tmdb_cache` | `(cache_id, vtype)` → 双语名/季集 JSON，TTL = `TMDB_META_TTL`（7 天）。cache_id 格式：`f"{source_kind}:{source_id}"` |
| `kv_cache` | 通用缓存 `(key, value JSON, expires_at)`。搜索 key = `search:{vtype}|{video_id}|{kw}|{disks}`；inspect key = `inspect:{share_url}|{passcode}`；TTL = `SEARCH_CACHE_TTL`（1 天）|
| `api_calls` | API 调用统计（kind / endpoint / duration / success / error / meta JSON / created_at），Admin 面板展示 |
| `play_count` | 按用户每日计数，Lite 用户配额判定 |

旧库自动迁移：`PRAGMA table_info` 检查 + `ALTER TABLE ADD COLUMN`。

---

## 7. 配置项

### 7.1 硬编码（`app/config.py`，环境变量 + .env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SECRET_KEY` | 必填 | Fernet 密钥（`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`）|
| `ADMIN_TOKEN` | 必填 | 全部管理 API 鉴权（前端存 localStorage）|
| `DB_PATH` | `data/quark_stremio.db` | SQLite 路径 |
| `HOST`/`PORT` | `0.0.0.0` / `8000` | 监听；生产 nohup 跑 80 |
| `APP_NAME` | `GoddamnCool!` | manifest + 页面标题 + 顶栏 |
| `TMDB_API_KEY` | 文件 / 环境 | 根目录 `TMDB_API_KEY` 或环境变量 |
| `TMDB_META_TTL` | `604800` | TMDB 缓存 7 天 |
| `MATCH_THRESHOLD` | `0.6` | 匹配置信度下限 |
| `PROXY_STREAM` | `0` | 0=播放直链（推荐）；1=代理流 |
| `BASE_URL` | 空 | 外部引导链接前缀 |
| `PANSOU_BASE_URL` | `127.0.0.1:8888` | 自部署 PanSou |
| `SEARCH_TIMEOUT` / `SEARCH_LIMIT` | `25` / `30` | 单来源超时 / 候选上限 |
| `SEARCH_CACHE_TTL` | `86400` | 搜索缓存 1 天；0 关闭 |
| `INSPECT_LIMIT` | `12` | 搜索时对"有效"候选并发 inspect 上限 |
| `TRANSFER_DIR` | `GODDAMNCOOL` | 转存目标目录（网盘根目录下）|
| `TRANSFER_SCAN_INTERVAL` | `10` | Worker 扫描间隔秒 |
| `TRANSFER_POLL_INTERVAL` | `2` | 夸克任务轮询秒 |
| `TRANSFER_TASK_TIMEOUT` | `600` | 轮询超时秒 → EXPIRED |
| `TRANSFER_MAX_ATTEMPTS` | `3` | 瞬时错误重试上限 |
| `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` | 空 | 三项全非空 `llm.configured()` 才返回 True |

### 7.2 Admin 面板可配（`app/settings.py`，SQLite kv 表）

| 配置键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `META_PROVIDER` | str | `tmdb` | 元数据源（当前唯一 tmdb，未来可加 douban）|
| `API_LOG_DETAILS` | bool | `false` | 开启记录完整 request/response 到 api_calls.meta |
| `ADDON_ICON` | str | 空 | Stremio manifest logo URL |
| `SEARCH_CACHE_TTL` | int | `86400` | 可覆盖 config |
| `LITE_DAILY_STREAMING_LIMIT` | int | 100 | Lite 用户每日 Stremio 播放配额 |

**settings.py 类型规则**：支持 int / float / str / bool（Python bool 和 `"true"` / `"false"` 字符串都能识别）。未知配置项走 admin 面板白名单过滤。

---

## 8. API 文档

### 8.1 Stremio Add-on（无鉴权）

| 路由 | 说明 |
|---|---|
| `GET /s/{stream_token}/manifest.json` | Addon 清单：`name=APP_NAME`、`resources=["stream"]`、`types=["movie","series"]`、`idPrefixes=["tt"]` |
| `GET /s/{stream_token}/stream/{movie\|series}/{id}.json` | 播放流。返回 `{"streams":[...]}` + `X-Addon-Status` header。`id` 形如 `tt0816692` 或 `tt5636724:1:2` |
| `GET /health` | 健康检查 |

### 8.2 Search / Admin API（需 `X-Admin-Token` 或登录 session）

| 路由 | 说明 |
|---|---|
| `GET  /api/search` | 搜索（vtype/id 锚定模式，kw 独立模式）|
| `POST /api/candidates/search` | 同上，POST 版本 |
| `POST /api/candidates/inspect` | 解析分享内容（files/seasons/video_count/is_series）|
| `POST /api/candidates/confirm` | 创建转存任务（mode=episode/whole/picks），返回 `task_id` |
| `POST /api/candidates/verify` | 批量存活检测（代理 PanSou /api/check/links）|
| `GET  /api/task/{id}` | 任务详情 |
| `GET  /admin/config` / `POST /admin/config` | Admin 面板配置读写 |
| `GET  /admin/users` | 用户列表 |
| `POST /admin/cache/clear` | 清理缓存（all / search / tmdb / llm）|
| `GET  /admin/api_stats` | API 调用统计（汇总 + 日趋势 + 最近 30 条 + 完整 request/response）|

### 8.3 页面路由（`web/` 静态文件）

| 路由 | 说明 |
|---|---|
| `/setup` | 账号绑定页（POST Cookie，Fernet 加密入库）|
| `/search` | 搜索页（独立入口 + Stremio 锚定兼容旧 `/candidates`）|
| `/task/{id}` | 转存任务状态页 |
| `/admin` | 管理员面板 |

---

## 9. 前端交互要点

- **卡片按钮规则**（夸克、非失效候选）：
  - 多视频 → 「转存全部剧集」(whole)
  - 锚定 series + 多视频 → 额外出现「转存当前剧集（SxxEyy）」(episode)
  - 锚定 movie + 多视频 → 额外出现「转存当前影片」(episode)
  - 详情未解析 → 不放转存按钮，仅「查看内容」
- **查看内容弹窗**：badge 行（季集 + 规格：4K/WEB-DL/H.265/AAC），文件行可多选。动态主按钮：点选部分 → `转存所选剧集（N）`(picks)；未选 → 按锚定/独立模式自动匹配 whole 或 episode。
- **存活筛选**：默认「有效」；bad / locked 沉底由后端排序保证。
- **防 XSS**：全部 DOM 赋值用 `textContent` 不用 `innerHTML`（搜索关键词、API 返回数据都要过）。
- **按钮显示**：common.css 有 `button{display:inline-flex}`，覆盖 UA `hidden` 样式，需要 CSS 显式 `display:none`。

---

## 10. 测试

```bash
cd /root/workspace && .venv/bin/python -m pytest -q    # → 245 passed
```

- 全部**离线 Mock**：httpx.MockTransport 模拟夸克 / PanSou / TMDB / CheckTMDB 接口序列。
- **monkeypatch 签名必须照抄真实函数签名**（曾因 `kw` vs `keyword` 掩盖过真实 bug）。
- StreamService / TransferWorker 用真实临时 DB 文件（`:memory:` 每连接空库不可用）。
- TMDB 客户端测试覆盖：双语解析、external_ids 补拿 imdb_id、SQLite 缓存、CheckTMDB IP 降级。
- 测试脚本列表：`tests/test_tmdb.py`、`test_stremio_api.py`、`test_transfer_worker.py`、`test_admin.py`、`test_candidate_api.py`、`test_matcher.py`、`test_name_parser.py`、`test_quark_provider.py`、`test_search_provider.py`、`test_user_panel.py`、`test_auth.py`、`test_llm.py`、`test_quota.py`。

### 生产级探测脚本

```bash
cd /root/workspace && timeout 90 .venv/bin/python scripts/test_tmdb_latency.py
```
验证 TMDB 正确返回 imdb_id + SQLite 缓存命中 → 网络快 600-2900 倍 + 端点时延（全部走 `api.tmdb.org` 域名，Cloudflare IPv6 直连）。

---

## 11. 已知限制

1. 仅夸克支持自动转存；其他网盘候选仅展示链接手动保存。
2. 整包转存不做逐文件防重：已有同名文件时夸克产生副本（原件仍在，播放不受影响）。
3. 新转存文件 raw 原画由夸克后台异步扫描，出现前先播转码 HLS。
4. PanSou 公开实例兜底波动大；自部署端口 8888 当前公网开放无鉴权（收紧：改 compose 回 127.0.0.1 或启用 AUTH_*）。
5. inspect 失败的"有效"候选保守保留状态。
6. 夸克 `/file/search` 偶发 HTTP 500（Provider 自动重试一次）。
7. Stremio stream 端点只给 IMDb ID + 可选季集号，**title/year/封面必须自己补全**（当前走 MetaProvider/TMDB）。
8. `api.themoviedb.org` 境内 DNS 污染，默认已切 `api.tmdb.org`（Cloudflare IPv6），但 TMDBMetaProvider 保留 CheckTMDB IP 降级链路。

---

## 12. 快速验证清单（改完代码后）

```bash
# 1. 回归
.venv/bin/python -m pytest -q                    # → 245 passed

# 2. 健康检查
curl -s http://127.0.0.1/health                 # → {"ok":true}

# 3. Manifest
curl -s http://127.0.0.1/manifest.json | grep '"name"'

# 4. TMDB 探测（独立验证 MetaProvider 链路）
timeout 60 .venv/bin/python scripts/test_tmdb_latency.py   # → 4 fetch_meta OK + 4 by_keyword OK

# 5. 重启
PID=$(ss -tlnp | grep ':80' | grep -oP 'pid=\K[0-9]+' | head -1)
kill -9 $PID 2>/dev/null; sleep 1
setsid nohup .venv/bin/python -m app.main > /tmp/app.log 2>&1 < /dev/null & sleep 2
ss -tlnp | grep ':80' | grep -oP 'pid=\K[0-9]+' | head -1

# 6. 浏览器（公网 IP）
#    /search → 标题/品牌正确；搜"余罪"默认只显"有效"
#    /s/{token}/manifest.json → 正确返回 resources/types/idPrefixes
#    /admin → 配置面板（需 ADMIN_TOKEN）
#    不要在线上点真实转存按钮（会产生真实副作用）
```

---

## 13. 踩坑速查表（精华）

| 坑 | 现象 | 根因 | 解法 |
|---|---|---|---|
| TMDB `/search/movie` 无 imdb_id | movie 永远返回空 imdb_id | TMDB v3 历史遗留，只有 `/find/{imdb_id}` 或 `/{id}/external_ids` 才返回 | movie 和 tv 都统一补调 `/{kind}/{tmdb_id}/external_ids` |
| Stremio stream 端点只给 IMDb ID | 无法直接拿标题匹配网盘资源 | Stremio Add-on 协议规范 | 用 MetaProvider.fetch_meta 自行补全元数据 |
| `api.themoviedb.org` 超时 | TMDB 直连挂 | 境内 DNS 污染 | 默认切 `api.tmdb.org`（Cloudflare IPv6），TMDBMetaProvider 保留 CheckTMDB IP 降级 |
| CheckTMDB hosts 表 IP 变化 | 降级链路失效 | TMDB CloudFront 节点会变 | hosts 表按两个域名同时匹配，默认 IPv6 直连应该永远不触发降级 |
| 测试 monkeypatch 打不到 | `AttributeError: module 'app.transfer_worker' has no attribute 'fetch_meta'` | 改了 import 链后测试直接 patch 模块属性 | 每个模块保留模块级 fetch_meta 包装函数，内部委托 `get_meta_provider()` |
| `settings.get` 不支持 bool | Admin 面板 `"true"` 读成字符串 | 之前只处理 int/float/str | settings.py 加 bool 类型校验（Python bool + `"true"/"false"` 字符串）|
| 裸集数文件名 `06.mkv` | 标题匹配必失败 | 文件名无剧名 | 任务短路：COMPLETED 转存任务按季集号直接定位文件 |
| `/file/v2/play/project` 不可用 | 返回 14018/15000 | 夸克批量接口不稳定 | 只支持单文件 `/file/v2/play` fid 逐个取 |
| 下载直链限速 | 客户端 <100KB/s | 非取链 IP 限速 | 只用 play 直链（CDN 分发、不绑 IP）|
| 浏览器验证 127.0.0.1 失败 | chrome-error | Stremio 用 WebRTC 本地 server | 必须用公网 IP `http://47.117.135.164` |
| 进程判断 `pgrep -f` | 命中 bash 自身 | grep 匹配 `app.main` 时包装也包含此字符串 | `ss -tlnp | grep ':80' | grep -oP 'pid=\K[0-9]+' | head -1` |
| **绝对禁止** | 所有涉及进程判断的地方 | `pgrep -f` / `ps aux \| grep` | 用 `ss -tlnp` 口诀 |
| **TV 端（Nuvio）播放夸克 CDN 返回 412** | PC/手机能播，TV 端黑屏/412 | Nuvio 对 `proxyHeaders.Cookie` 做 URL 解码，把 base64 的 `+` 变成空格，`Video-Auth` 失效 | 按 UA 区分：TV 端预编码 `+`→`%2B`（Nuvio 解码还原），PC 端保持 `+`；存储 Cookie 的 `%2B` 先还原为 `+`（CDN 只认 `+`） |

---

## 13.1 TV 端 Cookie 编码踩坑详细记录（2026-10）

### 现象
夸克 CDN 直链在桌面端/手机 Stremio 能正常播放，但在 TV 端（Nuvio 客户端）播放失败，
CDN 返回 HTTP 412（或 400）。

### 排查过程

**第 1 步：确认 proxyHeaders 是否被遵循**
- 无法抓包（客户端↔CDN 走 TLS），在服务器上搭 HTTP 调试端点 `/_debug_proxy`，
  stream 响应里注入带 `X-Debug-Proxy: yes-please` 标记头的假流。
- TV 端播放后看服务器日志，确认 `X-Debug-Proxy`、`Cookie`、`User-Agent`、`Referer`
  全部 ✅ 到达。**排除"TV 端不遵循 proxyHeaders"假设。**

**第 2 步：排除 Cookie 长度问题**
- 注入 1352 字符假 Cookie（全 `x`），TV 端日志显示长度完整。**排除长度截断。**

**第 3 步：排除 HTTPS 证书问题**
- 把 stream URL 从 `https://` 降为 `http://`，TV 端仍 412。**排除证书/TLS。**

**第 4 步：服务端代理捕获 TV 端真实请求**
- 搭 `/_quark_proxy` 端点，TV 端请求时服务器记录其完整请求头，再用同样的 Range
  转发到夸克 CDN。CDN 返回 206，但此方案因持续连接消耗服务器资源被弃用。

**第 5 步：对比 PC 与 TV 的 Cookie 传递差异（根因）**
- 在调试流里注入**真实夸克 Cookie**（含 `Video-Auth` base64 字段），分别让 PC/TV
  请求 `/_debug_proxy`，对比日志里的 Cookie 首尾字符：
  - PC 尾部：`...XGNou4+2+XeS87x3TFNdfzFtJlZB4e`（`+` 保持原样）
  - TV 尾部：`...XGNou4 2 XeS87x3TFNdfzFtJlZB4e`（`+` 变成了空格）
- **根因：Nuvio 对 Cookie 值做了 `application/x-www-form-urlencoded` 解码，
  把 `+` 当作空格。** `Video-Auth` 是 base64，含 `+`/`/`/`=`，`+` 变空格后签名失效。

**第 6 步：验证 CDN 对 `+`/`%2B`/空格的接受情况**
```
原始 Cookie（含 +）          → CDN 206 ✅
+ → %2B（全编码）            → CDN 400 ❌
+ → 空格（模拟 TV 解码结果） → CDN 400 ❌
```
CDN **只认 `+`，不认 `%2B` 也不认空格**。

**第 7 步：按 UA 区分客户端编码**
- PC 端 UA = `okhttp/4.12.0`，不做解码 → 保持原始 `+`
- TV 端 UA = `Nuvio/1.1.0-beta.8`，做解码 → 预编码 `+`→`%2B`，Nuvio 解码后还原为 `+`

**第 8 步：发现存储 Cookie 含 `%2B` 的二次问题**
- 修复 PC 端后 PC 又不能播了，排查发现数据库存储的 Cookie 本身含 5 个 `%2B`
  （夸克生成时已编码），CDN 不认 `%2B`。
- 增加规范化步骤：先把存储 Cookie 里所有 `%2B` 还原为 `+`，再按客户端决定是否编码。

### 最终修复（`app/stremio_routes.py`）

```python
ua = request.headers.get("user-agent", "")
cookie = db.load_user_cookie(config.DB_PATH, user["id"])
if cookie:
    cookie = cookie.replace("%2B", "+")  # 规范化：CDN 只认 + 不认 %2B
    if any(x in ua.lower() for x in ["nuvio", "android tv", "androidtv"]):
        cookie = cookie.replace("+", "%2B")  # TV 端预编码，Nuvio 解码还原
```

### 关键结论
1. **Nuvio 的 bug**：对 `behaviorHints.proxyHeaders` 里的字符串值做 URL 解码，
   导致 Cookie 中 base64 的 `+` 变成空格。PC 端 okhttp 无此问题。
2. **CDN 严格**：只接受原始 `+`，`%2B` 和空格都会返回 400/412。
3. **`Video-Auth` 是唯一必需 Cookie**：`__pus`、`__puus` 单独都不能通过 CDN 校验，
   必须传 `Video-Auth`，而它必然含 `+`。
4. **服务端代理方案不可行**：大文件流式转发会导致服务器持续建立到 CDN 的连接，
   资源消耗过大（SSH 卡死），不可作为生产方案。

### 排查此类问题的通用方法
1. **无法抓包时自建 HTTP 调试端点**：让客户端请求你的服务器，记录完整请求头，
   对比 PC/TV 差异。注意用最小假 mp4 响应，避免播放器反复请求刷屏。
2. **逐步二分排除**：proxyHeaders 遵循性 → Cookie 长度 → HTTPS → Cookie 特殊字符。
3. **对比首尾字符**：长字符串被篡改时，首尾对比比长度对比更敏感（长度可能不变
   但字符变了，如 `+`→空格）。
4. **CDN 对编码的容忍度要实测**：不要假设 CDN 会做 URL 解码，用 curl 直接验证。
5. **存储数据也要规范化**：第三方返回的数据可能已编码，入库后需统一规范化再使用。

---

## 14. 未来扩展建议

1. **暴露 catalog 端点**：manifest 加 `"catalog"`，给 Stremio 内部搜索。返回 Card 必须带 IMDb ID（Stremio 内部要关联自己的元数据），PanSou 结果先走 `fetch_meta_by_keyword` 匹配。
2. **DoubanMetaProvider**：豆瓣无官方 API，但有第三方镜像（如 movie.douban.com 的 HTML 解析）。实现 `MetaProvider` 接口，factory 加分支，admin 面板 META_PROVIDER 选项里加 `"douban"`——**所有调用方零改动**。
3. **多网盘转存**：Provider 抽象已就绪，加 BaiduDriveProvider / AliyunDriveProvider。
4. **LLM 增强搜索**：`llm.py` 配置已就绪，AISearchChain 已实现 SKIM + DEEPPARSE，在 admin 面板开 LLM_API_* 即启用。
5. **API_LOG_MAX_LEN**：当前是 config 内部常量 20000（admin 不可调），如果需要生产级别可做成 admin 配置项。
