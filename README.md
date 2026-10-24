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
app/
├── main.py            # FastAPI 组装与启动
├── config.py          # .env 配置加载
├── db.py              # SQLite：凭证（Fernet 加密）+ 转存任务表
├── tmdb.py            # IMDb ID → TMDB 双语标题/年份/季集（缓存 + CheckTMDB IP 直连）
├── matcher.py         # MovieMatcher 文件名解析与置信度匹配
├── stream_service.py  # Stream 业务编排（未命中时生成 externalUrl 引导流）
├── transfer_worker.py # 转存 Worker（后台执行转存任务：状态机推进/失败恢复/重启恢复）
├── candidate_service.py # 多来源候选搜索编排（去重/评分）
├── candidate_routes.py  # /candidates 候选页、/task/{id} 任务页、/api/* 鉴权接口
├── stremio_routes.py  # /manifest.json /stream/...
├── web_routes.py      # /setup /health
└── providers/
    ├── base.py        # CloudDriveProvider 抽象
    ├── quark.py       # 夸克私有接口封装（唯一标注"可能随官方变更"的文件）
    └── search/        # ResourceSearchProvider 抽象 + PanSou 实现（可插拔扩展）
```

技术方案与调研依据见 [docs/phase1-research.md](docs/phase1-research.md)。

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
   点击在浏览器打开候选资源页（也可直接访问 `/candidates?vtype=movie&id=tt...`）
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
10. **重复转存会产生自动改名副本**（如 `06(1).mkv`）：Worker 提交前已按"文件名+大小"
    防重检查规避；手动在网盘中重复转存仍会产生副本，需自行清理。

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
