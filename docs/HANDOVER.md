# GoddamnCool! 项目交接文档

> 夸克网盘 × Stremio 单用户自托管 Add-on：搜索 → 转存 → 播放一条龙。
> 本文档面向接手开发/运维的工程师，覆盖架构、配置、API、业务规则与踩坑记录。
> 使用说明速览见根目录 [README.md](../README.md)，原始需求见 [项目需求.md](../项目需求.md)。

---

## 1. 项目概述

GoddamnCool! 是一个纯 Python 的 Stremio Add-on，运行在自己的服务器上（当前部署于阿里云上海，公网 IP `47.117.135.164`，端口 80）。它把**用户自己的夸克网盘**变成 Stremio 的视频源：

1. **播放自己的网盘文件**：绑定夸克 Cookie 后，Stremio 里直接播放网盘中与影片标题匹配的文件（播放直链不经服务器转发，零流量消耗）。
2. **搜索全网网盘资源**：内置搜索页聚合 PanSou（自部署）多网盘分享链接，检测存活、解析内容（季集/规格/大小）。
3. **自动转存**：选中的分享由后台 Worker 自动转存到网盘 `GODDAMNCOOL` 文件夹，完成后 Stremio 刷新即播。

**产品名称**：`APP_NAME`（默认 `GoddamnCool!`），统一用于 manifest、页面标题、顶栏品牌。

### 功能阶段一览

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1-2 | 夸克 Provider、播放直链（play 接口）、Stremio 流对接 | ✅ |
| 3 | PanSou 外部检索、候选页、存活检测、双名搜索 | ✅ |
| 4 | 转存 Worker 六态状态机、防重、Stream 闭环 | ✅ |
| 5 | 分享内容详情（inspect）、整包/单集转存、规格解析 | ✅ |
| — | 前后端分离（web/ 静态文件）、TMDB 元数据替代 Cinemeta | ✅ |
| 6 | 独立搜索页 /search（双模式）、多网盘筛选、TV 遥控器适配 | ✅ |
| 7 | 搜索增强：详情直显、弹窗多选转存、kv 缓存 1 天、APP_NAME、硬朗 UI | ✅ |
| 7+ | UI 细节：规格 badge、卡片/弹窗按钮动态化、页面限宽 | ✅ |

---

## 2. 系统架构

```
Stremio 客户端 ──HTTP──▶ FastAPI (app/main.py, 端口 80)
                          │
                          ├─ stremio_routes  manifest.json / stream/{type}/{id}.json
                          │     └─ stream_service  网盘匹配 + 转存任务短路 + externalUrl
                          ├─ candidate_routes 搜索/确认/详情/检测 API（X-Admin-Token）
                          │     ├─ candidate_service ──▶ PanSou (docker, 127.0.0.1:8888)
                          │     │                    ──▶ TMDB (CheckTMDB IP 直连兜底)
                          │     │                    ──▶ QuarkDriveProvider（存活检测/inspect）
                          │     └─ db  transfer_tasks / kv_cache / tmdb_cache
                          ├─ transfer_worker  后台线程，六态状态机，夸克转存接口族
                          ├─ web_routes       /setup 页面 + JSON 契约
                          └─ web/ 静态文件     setup.html / search.html / task.html + js/css
                                     （/static 挂载，全部 no-store）
```

- **播放流量不经服务器**：`PROXY_STREAM=0`（默认）时返回夸克播放直链 + `proxyHeaders`（Cookie），客户端直连夸克 CDN。
- **服务器只出站查询**：搜索（PanSou/TMDB）、分享解析、转存提交均为普通 API 调用。

### 目录结构

```
app/
  main.py              FastAPI 入口（路由注册、/static 挂载、Worker 启动）
  config.py            全部配置（环境变量，含 APP_NAME/SEARCH_CACHE_TTL/INSPECT_LIMIT）
  db.py                SQLite：cookie/tasks/kv_cache/tmdb_cache + 加密
  tmdb.py              TMDB 元数据（find/season/episodes）+ CheckTMDB IP 直连兜底
  matcher.py           文件名 ↔ 影片标题匹配、季集解析、parse_file_spec 规格解析
  candidate_service.py 搜索编排（多关键词并发）、评分去重、存活检测、inspect、缓存
  candidate_routes.py  /search 页面路由 + /api/candidates/* + /api/task/*
  stremio_routes.py    Stremio manifest 与 stream 接口
  stream_service.py    流解析：网盘搜索匹配 → 转存任务短路 → externalUrl 引导
  transfer_worker.py   转存 Worker（asyncio 线程）：六态状态机
  web_routes.py        /setup 页面与提交（JSON 契约）、page() 模板注入 APP_NAME
  proxy.py             可选代理流（PROXY_STREAM=1 时使用）
  providers/
    base.py            CloudDriveProvider 抽象基类
    quark.py           夸克网盘 Provider（登录态、文件、分享、转存、play）
    search/base.py     ResourceSearchProvider / SearchCandidate / SearchError
    search/pansou.py   PanSou 搜索 + /api/check/links 存活检测
web/                   前端静态文件（无构建步骤，直接可改）
  setup.html / search.html / task.html
  css/common.css       深色绿色主题变量、按钮/表单/焦点样式（三页共用）
  css/search.css       搜索页专属（卡片/弹窗/文件清单）
  js/common.js         getToken/api/showMsg
  js/search.js         搜索页主逻辑（渲染/筛选/弹窗/转存）
  js/task.js setup.js  任务页/绑定页逻辑
  js/spatial-nav.js    TV 遥控器方向键空间导航
tests/                 pytest（149 个用例，全部离线 Mock，不触外网）
deploy/pansou/         PanSou docker-compose（自部署聚合搜索）
scripts/               客户端测速、夸克接口探针脚本（probe*.py）
data/                  SQLite 数据库（默认 data/quark_stremio.db）
docs/                  研究与交接文档（本文件）
```

---

## 3. 部署与运维

### 3.1 首次部署

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # 填 SECRET_KEY / ADMIN_TOKEN / QUARK_COOKIE(可选)
.venv/bin/python -m app.main  # 生产: nohup .venv/bin/python -m app.main > app.log 2>&1 &
```

- 数据库与加密密钥见 §4；夸克 Cookie 在 `/setup` 页粘贴导入（格式 `kps=..; sign=..; vcode=..; __puus=..`）。
- Stremio 安装：`https://<域名或IP>/manifest.json`。
- PanSou（可选但推荐）：

```bash
cd deploy/pansou && docker compose up -d   # 端口 8888（当前公网开放，无鉴权）
# 境内拉不动 ghcr.io 时用南大镜像:
docker pull ghcr.nju.edu.cn/fish2018/pansou-web:latest && docker tag ... ghcr.io/fish2018/pansou-web:latest
```

### 3.2 日常运维

```bash
# 查看服务（判断进程存活用方括号技巧，避免 grep 命中自身）
ss -tlnp | grep ':80 ' ; ps aux | grep "[a]pp.main"
# 重启
kill <PID> && nohup .venv/bin/python -m app.main > app.log 2>&1 &
# 健康检查
curl -s http://127.0.0.1/health
```

- 日志要点：`TMDB 连接失败，改用 CheckTMDB IP 直连`（正常兜底）、`转存任务` 状态流转、PanSou 重试。
- 排障口诀：**页面 200 但没有 /api/* 请求 = 前端 JS 没跑**（先看浏览器 Console 与 `<script>` 闭合）；**API 有数据 DOM 空 = JS 运行时异常**（找 ReferenceError）。

---

## 4. 配置项（.env，全部见 .env.example）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SECRET_KEY` | 必填 | Fernet 密钥，加密库中存储的夸克 Cookie |
| `ADMIN_TOKEN` | 必填 | `/setup` 与全部管理 API 的鉴权令牌（前端存 localStorage，走 `X-Admin-Token` 头） |
| `QUARK_COOKIE` | 空 | 初始 Cookie，也可页面导入 |
| `DB_PATH` | data/quark_stremio.db | SQLite 路径 |
| `HOST`/`PORT` | 0.0.0.0/8000 | 监听；生产 nohup 跑 80 |
| `MATCH_THRESHOLD` | 0.6 | 标题匹配置信度阈值，低于不返回播放源 |
| `PROXY_STREAM` | 0 | 0=播放直链（推荐）；1=服务器代理流（耗流量） |
| `BASE_URL` | 空 | 对外地址（代理流前缀 / externalUrl 引导链接） |
| `APP_NAME` | GoddamnCool! | 产品名（manifest + 页面标题 + 顶栏） |
| `TMDB_API_KEY` | 文件 | 优先环境变量，其次根目录 `TMDB_API_KEY` 文件 |
| `TMDB_META_TTL` | 604800 | TMDB 元数据缓存 7 天 |
| `PANSOU_BASE_URL` | 127.0.0.1:8888 | 自部署 PanSou |
| `SEARCH_TIMEOUT`/`SEARCH_LIMIT` | 25/30 | 单来源超时 / 候选上限 |
| `SEARCH_CACHE_TTL` | 86400 | 搜索结果与分享详情缓存 1 天；0 关闭 |
| `INSPECT_LIMIT` | 12 | 搜索时对"有效"候选并发解析详情的最大条数 |
| `TRANSFER_DIR` | GODDAMNCOOL | 转存目标（网盘根目录下文件夹） |
| `TRANSFER_SCAN_INTERVAL` | 10 | Worker 扫描间隔 |
| `TRANSFER_POLL_INTERVAL` | 2 | 夸克任务轮询间隔 |
| `TRANSFER_TASK_TIMEOUT` | 600 | 轮询超时 → EXPIRED |
| `TRANSFER_MAX_ATTEMPTS` | 3 | 瞬时错误重试上限 → FAILED |

---

## 5. 数据模型（SQLite, db.py）

| 表 | 说明 |
|---|---|
| `kv` | 单行配置（加密 Cookie 等） |
| `transfer_tasks` | 转存任务。关键列：`status`(六态)、`vtype/imdb_id`(锚定,可为空=share-only)、`season/episode`、`mode`(episode/whole/picks)、`picks`(fid JSON)、`share_url/passcode`、`target_fid`、`result_json`、`error`、`attempts`、`quark_task_id`。幂等键 = share+imdb+IFNULL(季/集,-1)+picks。**注意列名是 `video_type` 不是 `vtype`** |
| `tmdb_cache` | (imdb_id, vtype) → 双语名/季集 JSON，TTL 7 天 |
| `kv_cache` | 通用缓存 (key, value JSON, expires_at)。搜索 key=`search:{vtype}|{video_id}|{kw}|{disks}`；inspect key=`inspect:{share_url}|{passcode}`；TTL=SEARCH_CACHE_TTL |

旧库自动迁移：新增列走 `PRAGMA table_info` 检查 + ALTER（见 db.py）。

---

## 6. API 文档

### 6.1 Stremio 接口（无鉴权，Add-on 协议）

- `GET /manifest.json` — Addon 清单（name=APP_NAME，catalog 空占位）。
- `GET /stream/{movie|series}/{id}.json` — 返回匹配的播放流：
  - 网盘已有一集文件 → `url`=夸克播放直链 + `proxyHeaders.Cookie`；
  - 有 COMPLETED 转存任务且文件已定位 → 直接命中（reason="转存任务已定位该文件"）；
  - 都没有 → `externalUrl` 指向 `/search?vtype=&id=` 引导搜索转存。

### 6.2 管理 API（全部要求 `X-Admin-Token` 头）

| 接口 | 说明 |
|---|---|
| `POST /setup` | Form(token, cookie)：校验并绑定夸克 Cookie（校验失败不落库），返回 `{ok,message}` |
| `GET /api/candidates/search?vtype=&id=&kw=&disks=` | 搜索。vtype/id 成对=锚定模式（TMDB 双语名自动搜）；kw 必填=独立模式。disks: 缺省=仅夸克，`all`=全部，逗号列表=指定。返回 candidates（含 `alive_state` 与 `detail` 内容摘要） |
| `POST /api/candidates/confirm` | 创建转存任务。body: share_url/passcode/title/source/mode/vtype/id/season/episode/fids。mode=episode(单集)/whole(整包)/picks(fids 多选)。返回 task_id |
| `POST /api/candidates/inspect` | 解析分享内容：files[{name,path,fid,size,size_h,season,episode,spec}]、seasons、video_count、is_series（带缓存；无视频文件时搜索阶段直接标记失效） |
| `POST /api/candidates/verify` | 批量存活检测（代理 PanSou /api/check/links，单次 ≤40 条），返回 results[{share_url,state,summary}] |
| `GET /api/task/{id}` | 任务详情（含 status_text、save_dir、result_json） |

### 6.3 页面路由

`/setup`（绑定）、`/search`（搜索，兼容旧 `/candidates`）、`/task/{id}`（任务详情）。
全部 no-store；HTML 中 `{{APP_NAME}}` 由 `web_routes.page()` 注入。

---

## 7. 前端交互规则（web/js/search.js）

### 7.1 卡片按钮（夸克、非失效候选）

| 条件 | 按钮 |
|---|---|
| 多视频（detail.video_count>1） | 「转存全部剧集」(whole) |
| 单视频 | 「转存当前剧集」(whole，整包即该视频) |
| 锚定 series | 「转存当前剧集（SxxEyy）」(episode) + 多视频时加「转存全部剧集」 |
| 锚定 movie | 「转存当前影片」(episode) + 多视频时加「转存全部文件」 |
| detail 未解析（inspect 失败保守保留） | 不放转存按钮，仅「查看内容」 |

### 7.2 查看内容弹窗

- 顶部：灰字统计（`视频 N 个 · 共 X`）+ **badge 行**（季集 chips 与规格 badges 同行：`S02 · 12 集 · 18.5GB` `4K` `WEB-DL` `H.265` `AAC`）。
- 文件清单每行：文件名 + 大小·规格·季集（`1.5GB · 4K · WEB-DL · H.265 · AAC · S02E01`），点选可多选。
- 按钮：
  - **独立【转存全部剧集】**（多视频时显示，whole，无需手动全选）；
  - **动态主按钮**：点选部分 → `转存所选剧集（N）`(picks)；未选 → 锚定时`转存当前剧集（SxxEyy）`/`转存当前影片`(episode)，独立单视频时`转存当前剧集`(whole)，独立多视频时隐藏。
- Escape / 遮罩点击 / 关闭按钮均可关闭；弹窗打开时 TV 空间导航锁定在弹窗内。

### 7.3 其他

- 存活筛选默认「有效」（bad 沉底逻辑在后端排序）；来源级错误并入顶部 meta 小字。
- `spatial-nav.js`：方向键空间投影导航，`.frow` 上 Enter/空格触发 click，输入框内左右移动光标。
- 非夸克候选：宽松 URL 校验、不做存活检测、提示手动保存。

---

## 8. 关键业务规则（夸克接口要点）

### 8.1 播放直链（阶段 2 结论）

- `POST https://drive-pc.quark.cn/1/clouddrive/file/v2/play?pr=ucpro&fr=pc`，body `{"fid": "<fid>"}`（不带 `s`）。返回 `video_list`，取 `trans_status=="raw"` 条目为原画（SVIP 权益，非 SVIP 时 `accessable=False` 需跳过，降级 super 1080p）。
- 该直链**面向观看者分发、不绑定取链 IP**，无 Cookie 也能访问但慢；Stremio 经 `proxyHeaders` 携带 Cookie 后客户端实测 ~2.15MB/s。
- **CDN 签名覆盖完整 URI：直链不可追加任何查询参数（pr/fr 等），否则 403。**
- 新转存文件 raw 异步出现（先只有转码 HLS），Stream 自动降级 super，raw 出现后自动命中原画。
- 批量播放接口 `/file/v2/play/project` 不可用（14018/15000），只能单文件 fid 逐个取。
- `POST /file/download` 直链对非取链 IP 限速 <100KB/s 且大文件 23018，**不要用**。

### 8.2 转存接口族

`share/sharepage/token`(stoken；失效=HTTP404+业务码41011，需提取码=41008) → `share/sharepage/detail` → `share/sharepage/save`(task_id) → `task?task_id=&retry_index=`(status 2=成功)。
- 目录：`POST /file`（mkdir，同名 23008 → find_or_create_dir）；目标目录 `GODDAMNCOOL`。
- **防重**：提交前按"文件名+大小"搜网盘，命中跳过（夸克重复转存会产生 `06(1).mkv` 副本）。
- 定位不到目标文件时整包转存（宁占空间保播放，匹配交给 Stream）。

### 8.3 转存 Worker 状态机（transfer_worker.py）

```
PENDING → SUBMITTING → PROCESSING → COMPLETED / FAILED / EXPIRED
```
- 原子认领（`claim_task` WHERE status=from）；重启恢复（SUBMITTING 无 quark_task_id → 重跑，有 → 续轮询）。
- 永久错误（分享失效/提取码错误）直接 FAILED；瞬时错误重试 3 次；轮询超时 600s → EXPIRED。
- **_fail_transient 必须从 DB 重读当前状态**（局部变量与 DB 脱节会卡死 SUBMITTING）。
- mode=whole 跳过选片整包落盘（result_json.files 记录 ≤50 条清单）；mode=picks 按 fid 逐文件转存+防重。

### 8.4 匹配与规格（matcher.py）

- 裸集数文件名（`06.mkv`）标题匹配必失败，**必须靠任务短路**：`_BARE_EP_RE = ^(?:EP?[-_. ]?)?(\d{1,3})$`。
- `parse_file_spec` 返回**对象** `{resolution, source, codec, audio[], subtitle[], languages[]}`（不是数组！前端 formatSpec 负责格式化）。音频 token 尾边界用 `(?![a-z])` 而非 `\b`（否则 `DDP5.1` 识别丢失）。

### 8.5 检索与缓存

- PanSou：`POST /api/check/links`（注意不是 /api/check）批量检测；服务端自带检测缓存。公开实例 so.252035.xyz 仅作兜底（波动大）。
- 搜索并发搜全部 (provider, keyword)：锚定 = 手动 kw?/TMDB name_zh/name_en；独立 = 手动 kw。按 share_url 去重、评分排序、ok/locked 在前 bad 沉底。
- 搜索结果与 inspect 均走 kv_cache 缓存 1 天（仅无来源级错误才缓存）；TMDB 元数据缓存 7 天。
- TMDB 境内直连失败时自动 CheckTMDB IP 直连（SNI/Host 指向域名 + IP URL，等价 hosts；进程内缓存 24h）。

---

## 9. 测试

```bash
.venv/bin/python -m pytest -q   # 149 passed
```

- 全部离线：httpx.MockTransport 模拟夸克接口序列（FakeQuark）、FakeProvider 模拟搜索。
- **monkeypatch fake 的签名必须照抄真实函数签名**（曾因 `kw` vs `keyword` 掩盖过真实 bug）。
- StreamService 测试用真实临时 DB 文件（`:memory:` 每连接空库不可用）。
- 页面骨架测试：三页渲染/no-store/`<script>` 配对/引用的 /static 资源 200/APP_NAME 注入。

---

## 10. 已知问题与限制

1. 仅夸克支持自动转存；其他网盘候选仅展示链接手动保存。
2. 整包转存不做逐文件防重：网盘已有同名文件时产生 `06(1).mkv` 副本（原件仍在，播放不受影响）。
3. 新转存文件的原画（raw）由夸克后台异步扫描，出现前先播 1080p 转码版。
4. inspect 失败的"有效"候选保守保留状态（可能显示有效但点开内容才知道）；搜索阶段 INSPECT_LIMIT=12 条之外的候选不预解析。
5. PanSou 公开实例兜底波动大；自部署实例端口 8888 当前公网开放无鉴权（收紧：改 compose 回 127.0.0.1 或启用 AUTH_*）。
6. 夸克 `/file/search` 偶发 HTTP 500（Provider 已做一次 5xx 重试）。
7. 旧地址 `/candidates` 已兼容指向 /search，Stremio externalUrl 均为新地址。

---

## 11. 踩坑记录（精华，务必先读）

1. **前端"页面 200 但无 API 请求"= JS 没跑**：内嵌 HTML 曾丢 `</script>` 闭合；前后端分离后仍要警惕 script 标签配对（有回归测试）。
2. **参数名不一致型 TypeError**：路由传 `kw=` 而服务层参数是 `keyword`——第一步就 grep 两处源码对参数名，不要怀疑进程/缓存。
3. **锚定模式自动搜索被空关键词拦截**：`dosearch()` 的 `if (!kw) return` 必须写成 `if (!kw && !anchored)`，否则 `/search?vtype=&id=` 永远空白。
4. **spec 对象 vs 数组**：后端返回对象，前端 `f.spec.join()` 恒静默失败——显示异常先核对数据形状。
5. **`button[hidden]` 需要 CSS 显式 `display:none`**：common.css 的 `button{display:inline-flex}`（author）会覆盖 UA 的 hidden 样式。
6. **浏览器验证必须用公网 IP**（http://47.117.135.164），127.0.0.1 会 chrome-error；浏览器工具 evaluate 的 script 要写成 `return (function(){...})();` 才有返回值；fill 参数名是 `value`。
7. **进程/服务操作**：`pgrep -f app.main` 会命中 bash 包装自身，用 `ps aux | grep "[a]pp.main"`；kill 服务前先征求用户确认；后台任务杀进程用 TaskStop。
8. **git**：commit 用 `git -c user.name="chnnnnng" -c user.email="chnnnnng@users.noreply.github.com"`（不改全局配置）；境内 push 偶发 GnuTLS -110，失败不重试直接告知用户。
9. **整包转存副作用**：对已有终态任务的分享再点整包会真转存副本；验证前先查 transfer_tasks 同分享终态任务。
10. **浏览器缓存旧 JS**：所有页面与 /static 均 no-store，但排障时仍先强刷并核对响应头（"服务端已新、页面行为旧"多为此因）。

---

## 12. 快速验证清单（改完代码后）

1. `.venv/bin/python -m pytest -q` → 149 passed。
2. `curl -s http://127.0.0.1/manifest.json | grep name` → `"name": "GoddamnCool!"`。
3. 浏览器（公网 IP）：
   - `/search`：标题/品牌正确；搜"余罪"默认只显"有效"；卡片灰字统计 + badge 行（季集/4K/WEB-DL/…）；无 err 常驻框；foot 合规声明。
   - 任意卡片「查看内容」：弹窗 badge 行 + 文件行规格；多视频时有独立【转存全部剧集】按钮；点选文件行主按钮变「转存所选剧集（N）」、清空恢复；Escape 关闭。
   - 锚定 `/search?vtype=series&id=tt5925192:2:6`：自动搜索回填关键词；弹窗初始按钮「转存当前剧集（S02E06）」+「转存全部剧集」。
   - `/task/{id}`：无 Stremio 按钮，统一提示落盘目录。
4. **不要在线上点真实转存按钮**（会产生真实转存副作用，见 §11.9）。
