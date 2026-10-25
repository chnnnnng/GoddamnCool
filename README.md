# Quark Stremio Add-on

在 [Stremio](https://www.stremio.com/) 中播放您**个人夸克网盘**内已保存的影视资源。
单用户自托管服务，纯 Python 实现（不依赖 Node.js / stremio-addon-sdk）。

> 仅支持播放您自行保存到网盘的合法内容；请遵守当地法律法规与网盘服务条款。

## 功能

- **Stremio 协议**：`/manifest.json` + `/stream/{type}/{id}.json`（支持 movie / series，含 `tt...:S:E` 剧集定位）
- **元数据解析**：IMDb ID → [TMDB](https://www.themoviedb.org/) 双语标题/年份/季集
  （SQLite 缓存 7 天；境内 DNS 污染场景自动改用 [CheckTMDB](https://github.com/cnwikee/CheckTMDB)
  提供的 IP 直连，SNI/Host/证书校验仍指向原域名，无需改 /etc/hosts）
- **网盘检索与匹配**：夸克全盘搜索 + MovieMatcher 置信度匹配
  - 标题相似度（含**拼音跨语言**：`余罪` ↔ `Yu Zui`）
  - 年份硬否决（不同年份不错误匹配）、剧集集数硬匹配、预告/花絮/样片自动过滤
  - 置信度不足**宁缺毋滥**，不返回错误播放源
- **播放**：默认**播放直链模式**（`PROXY_STREAM=0`）——使用网页播放器同源接口
  `POST /file/v2/play` 取播放直链（域名 `video-play-c-*.drive.quark.cn`）。
  该域名面向"观看者"分发、不绑定取链 IP，客户端（Stremio 桌面端）可携带
  `proxyHeaders`（Cookie/UA/Referer）直连播放，服务器**不经手视频流量**；
  `trans_status=raw` 的条目即原画（`video_info.size` 与原文件一致），未播放过的文件同样可用，
  Range/206 正常可拖动。`trans_status=success` 的转码档（low/high 等）作为画质备选。
  设 `PROXY_STREAM=1` 可切换代理流模式（`/proxy/{fid}`，服务器带头取流转传，消耗服务器流量）
- **账号绑定**：Web 授权页粘贴 Cookie，Fernet 加密存 SQLite；`__puus` 自动回写防限流
- **外部检索与候选资源（阶段 3）**：网盘未命中时，Stream 响应附带 `externalUrl` 引导流
  （Stremio 标准字段，点击在浏览器打开候选资源页）；候选页聚合 [PanSou](https://github.com/fish2018/pansou)
  （MIT）搜索可转存的夸克分享资源：影片同时有中文名与外语名时（TMDB 双语名 /
  手动关键词）两个名字各搜一次并去重合并；搜索后自动调用 PanSou 链接检测 API
  （`/api/check/links`）标注存活状态，已失效与不确定的候选排序沉底。
  所有候选/任务接口均需 `X-Admin-Token` 鉴权，分享链接服务端强校验（仅夸克官方域）
- **自动转存与播放闭环（阶段 4）**：确认候选后创建转存任务（SQLite 持久化，六态状态机：
  PENDING/SUBMITTING/PROCESSING/COMPLETED/FAILED/EXPIRED），后台 Worker 自动解析分享、
  定位目标文件（精准匹配 → 裸集数启发式 → 整包兜底）、防重检查、提交并轮询转存；
  完成后 Stremio 刷新片源即可直接播放，任务页提供「在 Stremio 中播放」入口
- **分享内容详情与整包转存（阶段 5）**：候选卡片「查看内容」实时解析分享目录树，
  展示文件清单（大小、清晰度/来源/编码/音轨/字幕/语言规格徽章——从文件名标注解析）
  与剧集分季聚合；完整剧集分享提供「转存完整剧集」与「仅转存所选剧集」两种模式，
  整包任务落盘清单（fid/文件名/大小）记录进任务，Stream 按季集短路定位任意集
- **独立搜索页（阶段 6）**：`/search` 深色 + 绿色主题（硬朗方角风格）的完整搜索页，
  除承接 Stremio 外部搜索跳转（锚定模式）外，也可作为**独立搜索入口**使用——
  搜索框 + 网盘类型筛选（夸克/UC/百度/阿里/迅雷/115/123/天翼，服务端重搜）+
  存活状态筛选（客户端过滤，**默认只显示"有效"**）+ 结果卡片网格；
  UI 适配桌面端、移动端（44px 触摸目标）与 TV 端（遥控器方向键空间导航
  `js/spatial-nav.js`、焦点高亮、大屏字号放大）；旧地址 `/candidates` 仍兼容指向同页
- **搜索增强（阶段 7）**：搜索结果**单栏直显内容详情摘要**（视频数/总大小/分季信息）；
  「查看内容」改为**弹窗**展示完整文件清单（点选文件行可**多选**），并提供三种
  转存方式——【转存全部剧集】【转存所选剧集（多选）】【转存当前剧集（仅锚定剧集时）】；
  搜索后对"有效"候选自动解析内容，**无视频文件的链接标记为已失效**排除出"有效"；
  搜索结果与分享详情均缓存 **1 天**（`SEARCH_CACHE_TTL`，0 关闭）；
  任务页统一展示转存结果（不再区分是否关联 Stremio）；
  产品名称统一为 `APP_NAME`（默认 `GoddamnCool!`，页面标题/顶栏/Add-on 显示名）；
  搜索页底部常驻**合规声明**

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 生成配置
cp .env.example .env
# 编辑 .env：
#   SECRET_KEY  → python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   ADMIN_TOKEN → 自定义随机串，用于保护授权页

# 另需在项目根目录放置 TMDB_API_KEY 文件（TMDB v3 API key，一行纯文本），
# 或以环境变量 TMDB_API_KEY 提供

.venv/bin/python -m app.main        # 默认 0.0.0.0:8000，PORT 可改
```

### 绑定网盘账号

浏览器打开 `http://<host>:<port>/setup`，输入 ADMIN_TOKEN 与
[夸克 Cookie](docs/phase1-research.md)（登录 pan.quark.cn → F12 → Network → 复制任意请求的 Cookie 头）。

### 安装到 Stremio

桌面端打开：

```
stremio://<host>:<port>/manifest.json
```

> http 源仅桌面端/TV 可用（需要其内置服务器转发请求头）；Web 版浏览器无法自定义请求头，属预期限制。

## 架构

```
app/                  # 后端：纯 JSON API + 静态资源服务（前后端分离）
├── main.py            # FastAPI 组装与启动（/static 挂载前端静态资源，no-store）
├── config.py          # .env / 环境变量 / 根目录文件配置加载
├── settings.py        # Admin 面板可配置项（SQLite kv 表，支持 bool/int/float/str）
├── db.py              # SQLite：多用户表 + 凭证（Fernet 加密）+ 转存任务 + 缓存 + API 调用统计
├── auth.py            # Fernet 密钥、加密 / 解密 Cookie
├── meta/              # 元数据源域
│   ├── base.py        # MetaProvider 抽象 + VideoMeta 统一数据类
│   ├── tmdb.py        # TMDBMetaProvider 派生类（三路径 + SQLite 缓存）
│   └── __init__.py    # get_meta_provider() 工厂
├── search/            # 搜索域
│   ├── base.py        # ResourceSearchProvider / SearchCandidate / SearchError
│   ├── pansou.py      # PanSouProvider（PanSou 自部署实例 + /api/check/links）
│   ├── search_service.py  # 搜索编排（多关键词并发、去重、评分、缓存 1 天）
│   ├── search_chains.py   # BaseSearchChain / AISearchChain（AI 链叠加 SKIM→DEEPPARSE）
│   └── __init__.py
├── search_routes.py   # 搜索相关 HTTP 路由（/api/candidates/*，需鉴权）
├── stremio_routes.py  # /s/{stream_token}/manifest.json + /stream/{type}/{id}.json
├── stream_service.py  # Stream 业务编排：IMDb ID → MetaProvider → 网盘匹配 → Stremio Stream
├── transfer_worker.py # 转存 Worker（后台 asyncio 线程，六态状态机）
├── admin_routes.py    # Admin 面板 API（配置 / 用户 / 缓存 / API 统计）
├── user_routes.py     # 用户面板（stream_token、Cookie、配额）
├── auth_routes.py     # 用户登录 / 注册
├── web_routes.py      # /setup /search /task/{id} /admin 页面路由 + {{APP_NAME}} 注入
├── matcher.py         # MovieMatcher 文件名 ↔ 影片标题匹配（含拼音跨语言）
├── name_parser.py     # 文件名解析 + 规格徽章
├── llm.py             # AI 评审配置（API_BASE/API_KEY/MODEL 三项全非空才 configured）
└── providers/
    ├── base.py        # CloudDriveProvider 抽象
    ├── quark.py       # 夸克私有接口封装（登录态、文件、分享、转存、play 直链）
    └── __init__.py
web/                  # 前端：原生 HTML/CSS/JS 静态文件（无构建步骤，深色 + 绿色主题）
├── index.html         # 账号绑定页（/setup）
├── search.html        # 搜索页（独立搜索 + Stremio 锚定模式二合一）
├── task.html          # 转存任务状态页
├── admin.html         # 管理员面板
├── css/               # common.css（三页共用）+ search.css + admin.css
└── js/
    ├── common.js      # 令牌 / api() / DOM 工具（textContent 防 XSS）
    ├── spatial-nav.js # TV 遥控器方向键空间导航
    └── setup.js / search.js / task.js / admin.js
```

生产运维命令、全部配置项、私有接口要点、踩坑速查表见 [docs/HANDOVER.md](docs/HANDOVER.md)。

## 测试

```bash
.venv/bin/python -m pytest -q
```

覆盖：匹配器全部验收项（相似标题/年份否决/集数硬匹配/垃圾过滤/低置信度不返回/
跨语言拼音）、Stremio 端点契约（含未命中 externalUrl 引导与转存任务定位短路）、
夸克 Provider（MockTransport 模拟鉴权失败/`__puus` 回写/转存接口族）、PanSou Provider
（脏链接过滤/去重/错误降级）、候选页 API（鉴权/分享链接校验/任务幂等创建/六态状态机）、
转存 Worker（选片/防重/轮询/失败重试/重启恢复/旧库迁移）、TMDB 客户端（双语解析/
季集/缓存/IP 直连切换与瞬时重试）。

## 阶段 3 使用说明：候选资源与转存任务

1. Stremio 中打开一部**网盘没有**的影片 → 播放源列表出现"夸克网盘 · 外部搜索"，
   点击在浏览器打开搜索页（也可直接访问 `/search?vtype=movie&id=tt...`，
   旧地址 `/candidates?vtype=...` 仍兼容）
2. 首次使用输入 `ADMIN_TOKEN`（保存在浏览器 localStorage，不进 URL/日志）
3. 候选列表按匹配度评分排序（**已失效与不确定的沉底**），展示来源/发布时间/提取码与存活
   徽章（绿"有效" / 红"已失效"（卡片淡化） / 黄"需提取码或提取码错误"）。搜索关键词
   依次取：手动关键词 → TMDB 中文名 → TMDB 英文名（去重），影片有双语名时自动各搜一次
   并去重合并；存活检测由
   搜索流程统一完成（PanSou `/api/check/links`，其服务端自带检测缓存）；点击"确认转存到
   我的网盘"
4. 跳转任务页（`/task/{id}`）实时显示转存进度；完成后提供「在 Stremio 中播放」按钮
   （也可直接在 Stremio 刷新片源，自动命中转存文件，见下节）

配置项：`PANSOU_BASE_URL`（默认本地自部署实例）、`SEARCH_TIMEOUT`、`SEARCH_LIMIT`。

### 自部署 PanSou（推荐）

公开实例波动大（结果少、常超时），推荐用 Docker Compose 自部署**前后端集成版**
（`deploy/pansou/docker-compose.yml`）：

```bash
cd deploy/pansou
docker compose up -d          # Web UI: http://<服务器IP>:8888
curl -s -X POST http://127.0.0.1:8888/api/search \
  -H 'Content-Type: application/json' \
  -d '{"kw":"片名","res":"merge","cloud_types":["quark"]}'
```

> 境内服务器直连 ghcr.io 拉取镜像常卡死，可用南大镜像源：
> `docker pull ghcr.nju.edu.cn/fish2018/pansou-web:latest`
> `docker tag ghcr.nju.edu.cn/fish2018/pansou-web:latest ghcr.io/fish2018/pansou-web:latest`

实测对比（2026-10，同一关键词"余罪"）：自部署 ~4s 返回 216 条原始结果
（插件源国内可达），公开实例 26-32s（重试后）仅 3-14 条。

端口说明：默认 `"8888:80"` **公网开放**（云服务器上可直接浏览器访问 Web UI）；
PanSou 无鉴权，任何人可用此实例搜索（响应为 KB 级 JSON，流量成本低）。
如需收紧，把 compose 中端口改回 `"127.0.0.1:8888:80"` 仅本机访问，
或启用其 AUTH_* 认证环境变量（见官方 README）。

已知限制：PanSou 为第三方聚合索引（TG 频道 + 插件站点），结果可靠性不作保证，
仅供转存用户自有授权内容；个别插件返回的脏链接已在 Provider 端按夸克分享 URL
格式过滤；PanSou 必须用 POST 调用（GET 会被其 nginx 拒绝）；Provider 已做瞬时
失败重试 + 空结果自动补查；链接存活检测由 PanSou 服务端代查，PanSou 不可用时
候选徽章显示"不确定"，不影响转存操作本身。

## 阶段 4 使用说明：自动转存与播放闭环

转存 Worker 随服务启动（`app/transfer_worker.py`），自动扫描并执行 PENDING 任务：

1. **解析分享**：stoken → 分享文件清单（递归目录、自动解析提取码）
2. **定位目标文件**：精准匹配（标题+集数）→ 裸集数文件名启发式（`06.mkv`）→
   均失败则**转存整个分享**（宁占空间保播放，匹配交给 Stream 阶段）
3. **防重**：提交前按"文件名+大小"搜索网盘，命中则跳过转存直接标记完成
4. **执行与轮询**：转存到网盘根目录下 `TRANSFER_DIR`（默认 `GODDAMNCOOL`）文件夹（平铺），
   轮询直至完成；超时置 EXPIRED，永久错误（分享失效/提取码错误）与重试耗尽置 FAILED
5. **播放闭环**：完成后 Stream 请求多关键词搜索（TMDB 双语名 + 任务定位文件名）
   并信任 Worker 定位的文件，Stremio 刷新片源即可播放；裸集数文件名（`06.mkv`）
   这类标题匹配必失败的场景由任务定位短路兜住

服务重启自动恢复：SUBMITTING（无夸克任务号）回 PENDING 重跑，PROCESSING 续轮询。

配置项：`TRANSFER_DIR`、`TRANSFER_SCAN_INTERVAL`、`TRANSFER_POLL_INTERVAL`、
`TRANSFER_TASK_TIMEOUT`、`TRANSFER_MAX_ATTEMPTS`（见 `.env.example`）。

## 阶段 5 使用说明：分享内容详情与整包/单集转存

候选页每张有效候选卡片新增「查看内容」按钮（按需触发，实时查询夸克分享目录树）：

- **概览**：视频文件数、其他文件数、总大小；
- **剧集结构**：按季聚合标签（`S01 · 12 集 · 9.8GB`），文件行标注季集
  （裸集数文件名如 `01.mkv` 显示为 `E01`）；
- **文件规格**：从文件名标注解析清晰度（4K/1080p…）、来源（WEB-DL/BluRay…）、
  编码（H.264/H.265…）、音轨（DD+/5.1/FLAC…）、字幕（简体/繁体…）、语言
  （网盘文件无法读取媒体流，仅为文件名启发式，缺省字段省略）；
- **点选目标集**：文件行可点选（默认选中 Stremio 当前观看的集），
  完整剧集分享底部提供两个按钮：
  - **转存完整剧集**（`mode=whole`）：整包落盘，任务不锚定季集，
    对该剧任意集生效；落盘文件清单记录进任务，Stream 按季集短路定位，
    裸集数文件名（`06.mkv`）也能精准播放；
  - **仅转存所选剧集**（`mode=episode`）：只转存点选的那一集，
    按钮文字实时显示当前目标（如「仅转存所选剧集（E06）」）。

两种模式的任务可并存（幂等键不同）；电影分享不显示转存剧集按钮。

## 阶段 6 使用说明：独立搜索页

直接浏览器打开 `/search`（无需带影片参数）即可作为独立的网盘资源搜索入口：

- **搜索框**：输入片名/剧名/关键词回车或点「搜索」；
- **网盘筛选**（服务端重搜）：默认仅搜夸克（唯一可自动转存的网盘），可切
  「全部」或指定类型（UC/百度/阿里/迅雷/115/123/天翼）；非夸克资源仅展示
  来源与链接跳转，按钮区提示"仅支持打开链接手动保存"；
- **存活筛选**（客户端过滤）：全部 / 有效 / 需提取码 / 已失效 / 未检测，
  与搜索结果排序（失效与不确定沉底）配合快速定位可用资源；
- **转存**：夸克候选的「转存到我的网盘」创建 share-only 任务（不关联影片，
  任务页不显示 Stremio 播放入口，转存到网盘 `GODDAMNCOOL` 目录后直接在
  夸克网盘播放）；「查看内容」展开文件清单后可整包或按所选集转存；
- **多端适配**：桌面端/移动端单列卡片网格 + 44px 触摸目标；TV 端用
  遥控器方向键移动焦点（空间导航算法）、OK 键确认，大屏自动放大字号。

## 阶段 7 使用说明：搜索增强

- **单栏直显详情**：搜索结果单栏排列，有效候选的卡片直接展示内容摘要
  （视频文件数、总大小、分季信息 `S01 · 12 集 · 9.8GB`），无需先点开查看；
- **查看内容弹窗**：点击「查看内容」弹出独立窗口展示完整文件清单，
  **点选文件行可多选**，底部提供三种转存方式：
  - **转存全部剧集**（`mode=whole`）：整包落盘；
  - **转存所选剧集（N）**（`mode=picks`）：按多选的文件 fid 列表转存，
    未选择时按钮禁用；
  - **转存当前剧集（SxxEyy）**：仅在 Stremio 锚定剧集时出现，转存当前观看集；
  - 电影分享显示「转存全部文件」；TV 端弹窗内方向键导航（焦点锁定弹窗）、
    关闭键退出；
- **无视频内容排除**：搜索后对"有效"夸克候选（默认前 12 条，`INSPECT_TOP_N`）
  自动解析内容，不含任何视频文件的链接自动标记"已失效（内容无视频文件）"，
  配合默认的"有效"筛选实现排除；解析失败的保守保留原状态；
- **缓存**：搜索结果（含详情与存活状态）与分享详情均缓存 1 天
  （`SEARCH_CACHE_TTL=86400`，设 0 关闭）；带来源级错误的搜索结果不缓存；
- **任务页**：统一展示转存结果（状态/落盘目录「GODDAMNCOOL」），不再区分
  是否关联 Stremio，也不提供跳转按钮；
- **产品名称**：页面标题、顶栏品牌、Stremio Add-on 显示名统一取
  `APP_NAME`（默认 `GoddamnCool!`，`.env` 可改）；
- **合规声明**：搜索页底部常驻——本工具仅聚合第三方公开分享链接，不存储
  任何资源文件，请仅转存您拥有合法权利的内容。

## 已知问题与限制

1. **下载直链的限速与大小限制**：`POST /file/download`（dl-pc-* 域名）实测对非取链 IP
   软性限速（客户端直连 <100KB/s），且对大文件返回 `23018 download file size limit`。
   已改为默认使用播放直链（见上）；下载直链仅作回退。
2. **转码聚合接口不可用**：`POST /file/v2/play/project`（fids 批量）返回 `14018 plf_invalid`
   或 `15000 inner error`，与 OpenList 同参复现；单文件接口 `POST /file/v2/play`（body `{"fid":...}`）
   实测可用，已采用。
3. **原画条目为 SVIP 权益**（`right=svip`）：非 SVIP 账号会自动跳过 `accessable=False`
   的条目，回退可用的转码档。
4. **夸克瞬时 5xx**：`/file/search` 等接口偶发 HTTP 500，Provider 已做一次自动重试。
5. **跨语言检索**：TMDB 双语名各搜一次已解决大部分场景（中文名 ↔ 英文名）；网盘文件
   命名与双语名差异都较大时，候选页提供手动关键词搜索兜底（手动关键词不缓存，仅当次生效）。
6. **CDN 直链签名覆盖完整 URI**：转发直链时不可追加任何查询参数（如 API 的 pr/fr），
   否则 CDN 返回 403（见 `app/proxy.py` 注释）。
7. 所有夸克接口为**非官方私有接口**，可能随版本调整，需关注 `app/providers/quark.py`。
8. **新转存文件短期无原画条目**：夸克后台异步扫描转码，新转存文件短期内 play 接口
   仅返回转码 HLS 档（super/high/low，1080p 可正常播放），`trans_status=raw` 原画条目
   由夸克扫描完成后自动出现，此后 Stream 自动命中原画，无需人工处理。
9. **失效分享返回 HTTP 404 + 业务码 41011**（需要提取码为 41008）：Provider 层按业务码
   （而非 HTTP 状态）处理，分享失效的转存任务会快速置为 FAILED 并在任务页提示。
10. **重复转存会产生自动改名副本**（如 `06(1).mkv`）：单集任务提交前已按"文件名+大小"
    防重检查规避；**整包转存（阶段 5）不做逐文件防重**，网盘中已有部分文件时整包转存
    仍会为它们生成副本，需自行清理；副本文件名（`06(1).mkv`）无法被季集解析，但同名单
    原件仍在，播放定位不受影响。
    手动在网盘中重复转存同样会产生副本。

### 客户端直连测速

在**你自己的电脑**（非服务器）上验证播放直链真实速度：

```bash
bash scripts/client_speedtest.sh http://<host>:<port>        # Linux/macOS
powershell -File scripts/client_speedtest.ps1 -Addon http://<host>:<port>   # Windows
```

脚本会从 `/stream/...` 取播放直链并以 Stremio 同款请求头（Cookie/UA/Referer）直连下载采样，
对比无 Cookie 直连速度。

实测记录（2026-10，上海客户端 → 上海服务器取链）：带 Cookie **~2.15MB/s**（1080p 码率
约 0.33MB/s，余量 6 倍+），无 Cookie ~1.3KB/s（夸克对无凭证访问近乎封死）——
Cookie 是直连速度的关键，Stremio 桌面端经 `proxyHeaders` 自动携带。

## 许可证

AGPL-3.0
